package com.mikuagent.pet.memory

/**
 * 双端记忆同步的合并规则（手机侧）。
 *
 * **规则本身写在 `shared/sync_rules.json`** —— PC 的 `backend/sync_rules.py`
 * 实现同一套，两边的测试读同一个契约文件（`MergeRulesTest.kt` /
 * `tools/test_sync_rules.py`）。
 *
 * 为什么值得这么麻烦：合并规则两端不一致时，两边各自都「对」，只是一个覆盖了
 * 另一个；表现是「某台设备上的记录悄悄变了」，往往几天后才发现。
 *
 * 这个对象是**纯逻辑、不碰数据库**，所以能在 JVM 单测里跑（不用模拟器）。
 */
object MergeRules {

    /** 判定结果。字符串与 PC 侧同名同值，便于对照。 */
    const val INSERT = "insert"
    const val UPDATE = "update"
    const val KEEP = "keep"

    /**
     * 这条来料该不该落地。
     *
     * * 本地没有（null）→ 插入
     * * 对方**严格更新** → 覆盖
     * * 否则（含时间戳相等）→ 保留本地
     *
     * 时间戳相等时保留本地是刻意的：「同一份数据重复同步」结果必须不变（幂等），
     * 而重复同步一定会发生 —— 游标会回退 [watermarkOverlapSeconds] 重扫，
     * 就是为了容忍两台设备的时钟偏差。
     */
    fun decide(localUpdatedAt: Double?, incomingUpdatedAt: Double): String {
        if (localUpdatedAt == null) return INSERT
        return if (incomingUpdatedAt > localUpdatedAt) UPDATE else KEEP
    }

    /**
     * 会话按「天」分组：`YYYY-MM-DD HH:MM:SS` → `YYYY-MM-DD`。
     *
     * 与 PC `MemoryStore.get_or_create_today()` 的 `substr(created_at,1,10)`
     * 是同一个键 —— 同步两端都按它归并，所以不需要做会话 id ↔ uuid 的映射。
     */
    fun dayOf(createdAt: String?): String =
        (createdAt ?: "").take(10)

    /** [pickPrimary] 的入参。 */
    data class SessionRef(
        val id: Long,
        val createdAt: String,
        val messageCount: Int,
    )

    /**
     * 同一天可能有多条会话（历史碎片）→ 选出消息要并进哪一条。
     *
     * 规则：消息最多的优先；并列取创建更早的；再并列取 id 最小的。
     * 必须**完全确定**，否则两端会各自挑一条，消息就永远分在两处。
     */
    fun pickPrimary(candidates: List<SessionRef>): SessionRef? =
        candidates.sortedWith(
            compareBy({ -it.messageCount }, { it.createdAt }, { it.id })
        ).firstOrNull()

    /** 游标回退窗口（秒）。与契约文件里的 `watermark_overlap_seconds` 一致。 */
    const val WATERMARK_OVERLAP_SECONDS: Double = 300.0

    /** 单次拉取上限。 */
    const val PLAN_LIMIT: Int = 2000

    /** 单次推送分片。 */
    const val PUSH_CHUNK: Int = 500

    /** 拉取最多循环几轮（防对端 `has_more` 永远为真）。 */
    const val MAX_PULL_ROUNDS: Int = 10

    /**
     * 推/拉游标回退一个窗口。
     *
     * 手机与电脑的时钟不可能完全一致（也没对时）。严格用「上次同步时对方的时间」
     * 当游标的话，时钟慢的那台刚写下的行会被永久跳过。回退 300 秒重扫一遍，
     * 配合 [decide] 的幂等性，代价只是多传几行。
     */
    fun rewind(watermark: Double): Double =
        maxOf(0.0, watermark - WATERMARK_OVERLAP_SECONDS)
}
