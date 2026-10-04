package com.mikuagent.pet.brain

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * TTS 前的文本清洗、分句与语速补偿，必须与 PC **逐字/逐值一致**。
 *
 * 金标准由 PC 生成：`tools/tts_text_golden.json`
 * （`tools/test_tts_text.py --update`）。移植错了**不会报错** ——
 * 只会「念出来怪怪的」，甚至整句变静音（清洗后没有可用字符），
 * 所以这里逐条钉住。
 */
class TtsTextTest {

    private val golden: JSONObject by lazy {
        JSONObject(findRepoFile("tools/tts_text_golden.json").readText(Charsets.UTF_8))
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
    fun `文本清洗与 PC 一致`() {
        val cases = golden.getJSONArray("normalize")
        assertTrue(cases.length() > 0)
        val problems = mutableListOf<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val input = c.getString("input")
            val expected = c.getString("expect")
            val got = TtsText.normalize(input)
            if (got != expected) {
                problems += "输入 ${input.escape()}\n  期望 ${expected.escape()}\n  实得 ${got.escape()}"
            }
        }
        assertTrue("${problems.size} 条与 PC 不一致：\n" + problems.take(4).joinToString("\n"), problems.isEmpty())
    }

    @Test
    fun `分句与 PC 一致`() {
        val cases = golden.getJSONArray("split")
        assertTrue(cases.length() > 0)
        val problems = mutableListOf<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val input = c.getString("input")
            val maxLen = c.getInt("max_len")
            val raw = c.getJSONArray("expect")
            val expected = (0 until raw.length()).map { raw.getString(it) }
            val got = TtsText.splitSentences(input, maxLen)
            if (got != expected) {
                problems += "输入 ${input.escape()} max=$maxLen\n  期望 $expected\n  实得 $got"
            }
        }
        assertTrue("${problems.size} 条与 PC 不一致：\n" + problems.take(4).joinToString("\n"), problems.isEmpty())
    }

    @Test
    fun `语速补偿与 PC 一致`() {
        val cases = golden.getJSONArray("speed")
        assertTrue(cases.length() > 0)
        val problems = mutableListOf<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val got = TtsText.minimaxSpeed(
                baseSpeed = c.getDouble("base_speed"),
                mmEmotion = c.getString("mm_emotion"),
                biasSource = c.getString("text"),
                emotionLabel = c.getString("emotion"),
            )
            val expected = c.getDouble("expect")
            if (Math.abs(got - expected) > 1e-9) {
                problems += "${c.getString("mm_emotion")}/${c.getString("emotion")} " +
                    "期望 $expected 实得 $got"
            }
        }
        assertTrue("${problems.size} 条与 PC 不一致：\n" + problems.take(4).joinToString("\n"), problems.isEmpty())
    }

    @Test
    fun `整句都是装饰符号时视为无内容`() {
        // 这条单独拎出来：清洗后为空意味着「这一句不合成」，
        // 若实现成空串继续送给 TTS，会白花一次计费还得到一段静音。
        assertEquals("", TtsText.normalize("♪♪♪ ☆☆"))
        assertEquals("", TtsText.normalize("……"))
        assertEquals("", TtsText.normalize("こんにちは、ミクです"))
    }

    private fun String.escape(): String = replace("\n", "\\n").replace("\r", "\\r")
}
