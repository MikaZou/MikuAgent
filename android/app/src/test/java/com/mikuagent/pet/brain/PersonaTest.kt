package com.mikuagent.pet.brain

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * 人设提示词的**跨语言**逐字比对。
 *
 * 金标准不是在这里写的，而是 PC 侧生成的：`tools/persona_golden.json`
 * （由 `tools/test_persona_parity.py --update` 产出，32 个输入组合，
 * 记录 `shared/persona.txt` 的 sha1）。
 *
 * 为什么要这么麻烦：提示词现在两端各渲染一次。任何一边对空行/区块的处理出错，
 * 表现都只是「模型说话有点怪」—— 没有任何报错，极难发现。逐字比对是唯一
 * 能在提交前拦住它的办法。
 */
class PersonaTest {

    private val templateFile = Persona.findRepoFile("shared/persona.txt")
    private val goldenFile = Persona.findRepoFile("tools/persona_golden.json")

    private fun template(): String =
        templateFile.readText(Charsets.UTF_8)
            .replace("\r\n", "\n").replace('\r', '\n').removeSuffix("\n")

    @Test
    fun `模板与 golden 来自同一份文件`() {
        val golden = JSONObject(goldenFile.readText(Charsets.UTF_8))
        val want = golden.getString("template_sha1")
        val got = Persona.sha1(templateFile.readBytes())
        assertEquals(
            "shared/persona.txt 改过但 golden 没重建（跑 tools/test_persona_parity.py --update）",
            want, got,
        )
    }

    @Test
    fun `32 个组合全部逐字一致`() {
        val golden = JSONObject(goldenFile.readText(Charsets.UTF_8))
        val cases = golden.getJSONArray("cases")
        assertTrue("golden 里没有用例", cases.length() > 0)

        val tpl = template()
        val problems = mutableListOf<String>()

        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val userName = c.getString("user_name")
            val memoryText = c.getString("memory_text")
            val vision = c.getBoolean("vision")
            val platform = c.getString("platform")
            val note = c.getString("extra_note")
            val expected = c.getString("expected")

            // 与 Persona.buildSystemPrompt 相同的规则（那边只是多了一步读 assets）
            val got = Persona.render(
                template = tpl,
                sections = mapOf(
                    "user" to userName.isNotEmpty(),
                    "memory" to memoryText.trim().isNotEmpty(),
                    "vision" to vision,
                    "note" to note.trim().isNotEmpty(),
                    "hint_pc" to (platform != "phone"),
                    "hint_phone" to (platform == "phone"),
                ),
                values = mapOf(
                    "USER_NAME" to userName,
                    "MEMORY_TEXT" to memoryText.trim(),
                    "NOTE_TEXT" to note.trim(),
                ),
            )

            if (got != expected) {
                val at = expected.indices.firstOrNull { it >= got.length || expected[it] != got[it] }
                    ?: minOf(expected.length, got.length)
                problems += "case#$i user='$userName' mem='$memoryText' vision=$vision " +
                    "platform=$platform note='$note'\n" +
                    "  第 $at 个字符起不同：\n" +
                    "  golden=${expected.substring(at, minOf(at + 60, expected.length)).escape()}\n" +
                    "  实际  =${got.substring(at, minOf(at + 60, got.length)).escape()}"
            }
        }

        assertTrue(
            "${problems.size} / ${cases.length()} 个用例与 PC 渲染结果不一致：\n" +
                problems.take(3).joinToString("\n"),
            problems.isEmpty(),
        )
    }

    @Test
    fun `区块关闭时不会留下空行`() {
        // 这是最容易出错、又最不容易被看出来的地方：
        // 区块删掉之后，标记前后那些换行必须仍然拼出「原来就那样」的结果。
        val tpl = template()
        val bare = Persona.render(
            tpl,
            mapOf(
                "user" to false, "memory" to false, "vision" to false, "note" to false,
                "hint_pc" to true, "hint_phone" to false,
            ),
            mapOf("USER_NAME" to "", "MEMORY_TEXT" to "", "NOTE_TEXT" to ""),
        )
        assertTrue("不该留下未替换的占位符", !bare.contains("{"))
        assertTrue("不该留下区块标记", !bare.contains("{{"))
        assertTrue("全关时正文后面直接是记忆工具，只隔一个空行", bare.contains("正文里。\n\n【记忆工具】"))
        assertTrue("模板末尾不该有多余空行", bare.endsWith("色情内容。"))
    }

    private fun String.escape(): String = replace("\n", "\\n").replace("\r", "\\r")
}
