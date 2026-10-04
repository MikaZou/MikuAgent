package com.mikuagent.pet.brain

import android.content.Context
import java.io.File
import java.security.MessageDigest

/**
 * 人设提示词：渲染 `assets/persona.txt`。
 *
 * 模板文件来自仓库根的 `shared/`（见 app/build.gradle.kts 的 assets.srcDir），
 * PC 的 `backend/persona.py` 读的是**同一个文件** —— 人设只有一份。
 * 两端渲染规则也一样，只有两条：
 *
 * ```
 * {{#名字}} … {{/名字}}   可选区块；关闭时整段（含标记）删掉
 * {占位符}                必填值，直接替换
 * ```
 *
 * 区块标记写在**行内**，所以换行数完全由模板文本决定，渲染器不需要对空行
 * 做任何「聪明」的修补。这一点是刻意的：PC 那边原来是 `"\n".join(parts)`，
 * 不同组合下空行数并不一致（有长期记忆时后面跟三个换行），
 * 把换行写进模板才能做到**逐字相同**。
 *
 * 逐字相同由 `PersonaTest` 保证 —— 它读 PC 生成的金标准
 * `tools/persona_golden.json`（32 个组合）逐字比对。
 */
object Persona {

    private const val ASSET_NAME = "persona.txt"

    /** 区块标记。非贪婪 + 反向引用闭合名，避免 `{{#a}}…{{/b}}` 被错配。 */
    private val SECTION = Regex(
        """\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}""",
        RegexOption.DOT_MATCHES_ALL,
    )

    @Volatile
    private var cached: String? = null

    /**
     * 读模板。CRLF 归一成 LF —— Kotlin 的 `readText` 不像 Python 的文本模式
     * 那样自动转换，模板一旦被 Windows 工具改成 CRLF，`\r` 会原样混进提示词。
     */
    fun template(context: Context): String {
        cached?.let { return it }
        synchronized(this) {
            cached?.let { return it }
            val raw = context.assets.open(ASSET_NAME).bufferedReader(Charsets.UTF_8).use { it.readText() }
            val body = raw.replace("\r\n", "\n").replace('\r', '\n').removeSuffix("\n")
            cached = body
            return body
        }
    }

    /**
     * 渲染（纯函数，可在 JVM 单测里直接调）。
     *
     * 顺序不能反：先区块、后占位符。反过来的话，占位符的值里万一出现
     * `{{#…}}` 就会被当成区块解析；而区块删掉之后它内部的占位符自然也不用替换。
     *
     * 注意用 `Regex.replace(transform)` 这个重载 —— 它把返回值当**字面量**，
     * 不会解释 `$1` 之类；用字符串替换的那个重载会被模板里的 `$` 咬到。
     */
    fun render(
        template: String,
        sections: Map<String, Boolean>,
        values: Map<String, String>,
    ): String {
        val replaced = SECTION.replace(template) { m ->
            if (sections[m.groupValues[1]] == true) m.groupValues[2] else ""
        }
        var out = replaced
        for ((key, value) in values) {
            out = out.replace("{$key}", value)
        }
        return out
    }

    /**
     * 手机端组装系统提示词。参数含义与 `backend/persona.py::build_system_prompt` 一一对应。
     *
     * @param platform 决定「怎么让你看到画面」那句用哪套文案。写错了她会指挥
     *                 用户去点一个当前端上不存在的按钮（PC 端踩过这个坑）。
     */
    fun buildSystemPrompt(
        context: Context,
        userName: String? = null,
        memoryText: String = "",
        extraNote: String = "",
        vision: Boolean = false,
        platform: String = "phone",
    ): String {
        val isPhone = platform == "phone"
        return render(
            template = template(context),
            sections = mapOf(
                "user" to !userName.isNullOrEmpty(),
                "memory" to memoryText.trim().isNotEmpty(),
                "vision" to vision,
                "note" to extraNote.trim().isNotEmpty(),
                "hint_pc" to !isPhone,
                "hint_phone" to isPhone,
            ),
            values = mapOf(
                "USER_NAME" to (userName ?: ""),
                "MEMORY_TEXT" to memoryText.trim(),
                "NOTE_TEXT" to extraNote.trim(),
            ),
        )
    }

    /**
     * 供单测/排查用：模板的 sha1，和 golden 里记的对比就能确认读的是同一份。
     *
     * 入参是**原始字节**而不是字符串 —— PC 那边 `hashlib.sha1(TEMPLATE.read_bytes())`
     * 算的也是文件字节。若在这里做 CRLF 归一或去尾换行，算出来的就不一样了，
     * 这条「读的是同一份」的校验会直接失效。
     */
    fun sha1(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-1").digest(bytes)
            .joinToString("") { "%02x".format(it) }

    /** 单测里从磁盘找模板（没有 Context 可用时）。 */
    internal fun findRepoFile(relative: String): File {
        var dir: File? = File(".").absoluteFile
        repeat(6) {
            val candidate = File(dir, relative)
            if (candidate.exists()) return candidate
            dir = dir?.parentFile
        }
        throw IllegalStateException("在仓库里找不到 $relative（从 ${File(".").absolutePath} 往上找）")
    }
}
