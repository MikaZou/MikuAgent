package com.mikuagent.pet.brain

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * 情感标签解析必须与 PC **逐字一致**。
 *
 * 金标准由 PC 生成：`tools/emotion_parse_golden.json`
 * （`tools/test_emotion_parse.py --update`），PC 侧读同一份。
 *
 * 这个函数只有二十来行，却已经咬过两次（返回值的 `Pair` 顺序被写反、
 * 模型偶尔写不带方括号的裸标签），所以值得单独钉住。
 */
class EmotionParseTest {

    private val golden: JSONObject by lazy {
        JSONObject(findRepoFile("tools/emotion_parse_golden.json").readText(Charsets.UTF_8))
    }

    private fun findRepoFile(relative: String): File {
        var dir: File? = File(".").absoluteFile
        repeat(6) {
            val candidate = File(dir, relative)
            if (candidate.exists()) return candidate
            dir = dir?.parentFile
        }
        throw IllegalStateException("在仓库里找不到 $relative（从 ${File(".").absolutePath} 往上找）")
    }

    @Test
    fun `情感与正文都与 PC 一致`() {
        val cases = golden.getJSONArray("cases")
        assertTrue(cases.length() > 0)
        val problems = mutableListOf<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val input = c.getString("input")
            val parsed = Agent.parseEmotion(input)
            val wantEmotion = c.getString("emotion")
            val wantReply = c.getString("reply")
            if (parsed.emotion != wantEmotion || parsed.reply != wantReply) {
                problems += "输入 ${input.escape()}\n" +
                    "  期望 ${wantEmotion.escape()} / ${wantReply.escape()}\n" +
                    "  实得 ${parsed.emotion.escape()} / ${parsed.reply.escape()}"
            }
        }
        assertTrue(
            "${problems.size} 条与 PC 不一致：\n" + problems.take(4).joinToString("\n"),
            problems.isEmpty(),
        )
    }

    @Test
    fun `裸标签被当成情感且不留在正文里`() {
        // 这次踩到的原样输入（真机上 PC 回的正文里带着 "HAPPY"）
        val parsed = Agent.parseEmotion("HAPPY 收到收到～PC 这条路走得通だよ！☆")
        assertEquals("HAPPY", parsed.emotion)
        assertTrue("正文不该还带标签：${parsed.reply}", parsed.reply.startsWith("收到收到"))
    }

    @Test
    fun `不该误伤正文里的同类词`() {
        // 出现 "HAPPY" 但不在开头、或者后面不跟空白时，都不能被吞掉
        val a = Agent.parseEmotion("HAPPYS 这种更长的单词不该被切开")
        assertEquals("NORMAL", a.emotion)
        assertTrue(a.reply.startsWith("HAPPYS"))
        val b = Agent.parseEmotion("主人说 [1] 和 [2] 这种数字方括号不该被动")
        assertEquals("NORMAL", b.emotion)
        assertTrue("[1]" in b.reply)
    }

    private fun String.escape(): String = replace("\n", "\\n").replace("\r", "\\r")
}
