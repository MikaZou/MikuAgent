package com.mikuagent.pet.memory

/**
 * 记忆库与同步用的数据模型。
 *
 * 字段与 PC `backend/memory.py` 的四张表**一一对应** —— 两端各存一份同样的数据，
 * 同步时按 `uuid` 认领、按 `updated_at` 比新旧、用 `deleted` 当墓碑。
 *
 * 时间约定（两端必须一致）：
 * * `createdAt` 是**本地时间字符串** `YYYY-MM-DD HH:MM:SS`。会话按「天」分组靠的
 *   就是它的前 10 个字符（见 [MergeRules.dayOf]），所以格式错了会直接导致
 *   两端分到不同的会话里。
 * * `updatedAt` 是 epoch **秒**（带小数），用于 LWW 与同步游标。
 */

data class SessionRow(
    val id: Long,
    val uuid: String,
    val title: String,
    val createdAt: String,
    val updatedAt: Double,
    val deleted: Boolean,
    val origin: String,
)

data class MessageRow(
    val id: Long,
    val uuid: String,
    val sessionId: Long,
    val role: String,
    val content: String,
    /** 助手消息的情感标签；用户消息为 null。 */
    val emotion: String?,
    val createdAt: String,
    val updatedAt: Double,
    val deleted: Boolean,
    val origin: String,
)

data class MemoryRow(
    val id: Long,
    val uuid: String,
    val content: String,
    val category: String,
    val importance: Int,
    val source: String?,
    val createdAt: String,
    val updatedAt: Double,
    val deleted: Boolean,
    val origin: String,
)

data class MetaRow(
    val value: String,
    val updatedAt: Double,
    val deleted: Boolean,
)

/** 一天的会话连同这一天里变动过的消息。同步的**单位就是「天」**。 */
data class DayBundle(
    val day: String,
    val title: String,
    val updatedAt: Double,
    val deleted: Boolean,
    val messages: List<MessageRow>,
)

/** 一次同步要交换的全部内容。 */
data class SyncBundle(
    val days: List<DayBundle> = emptyList(),
    val memories: List<MemoryRow> = emptyList(),
    val meta: Map<String, MetaRow> = emptyMap(),
) {
    val messageCount: Int get() = days.sumOf { it.messages.size }

    val isEmpty: Boolean
        get() = days.isEmpty() && memories.isEmpty() && meta.isEmpty()
}

data class ApplyResult(
    val inserted: Int,
    val updated: Int,
    val kept: Int,
    val daysCreated: Int,
) {
    val touched: Int get() = inserted + updated
}

/** 同步游标与最后一次同步的结果（设置面板会显示，排查时也靠它）。 */
data class SyncState(
    val pullWatermark: Double,
    val pushWatermark: Double,
    val lastSyncAt: Double,
    val lastPullCount: Int,
    val lastPushCount: Int,
    val lastError: String,
) {
    companion object {
        val EMPTY = SyncState(0.0, 0.0, 0.0, 0, 0, "")
    }
}

/** 会话列表用（带消息条数与最后活跃时间，与 PC 的 list_sessions 同形状）。 */
data class SessionSummary(
    val id: Long,
    val title: String,
    val createdAt: String,
    val messageCount: Int,
    val lastActive: String?,
)
