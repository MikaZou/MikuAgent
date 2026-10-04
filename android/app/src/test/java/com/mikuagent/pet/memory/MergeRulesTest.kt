package com.mikuagent.pet.memory

import org.json.JSONObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * 合并规则必须与 `shared/sync_rules.json` 契约逐条一致。
 *
 * PC 的 `tools/test_sync_rules.py` 读**同一个**契约文件。两边都对着它跑，
 * 就不会出现「两端各自都觉得自己对」的静默分歧 —— 那是这类同步代码里最贵的 bug。
 */
class MergeRulesTest {

    private val contract: JSONObject by lazy {
        JSONObject(findRepoFile("shared/sync_rules.json").readText(Charsets.UTF_8))
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
    fun `decide 与契约一致`() {
        val cases = contract.getJSONArray("decide")
        assertTrue(cases.length() > 0)
        val seen = mutableSetOf<String>()
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val localTs = if (c.isNull("local")) null else c.getJSONObject("local").getDouble("updated_at")
            val incomingTs = c.getJSONObject("incoming").getDouble("updated_at")
            val got = MergeRules.decide(localTs, incomingTs)
            seen += c.getString("expect")
            assertEquals(c.getString("name"), c.getString("expect"), got)
        }
        assertEquals("契约要覆盖三种结果", setOf("insert", "update", "keep"), seen)
    }

    @Test
    fun `pickPrimary 与契约一致`() {
        val cases = contract.getJSONArray("pick_primary")
        assertTrue(cases.length() > 0)
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            val raw = c.getJSONArray("candidates")
            val candidates = (0 until raw.length()).map { k ->
                val o = raw.getJSONObject(k)
                MergeRules.SessionRef(
                    id = o.getLong("id"),
                    createdAt = o.getString("created_at"),
                    messageCount = o.getInt("message_count"),
                )
            }
            val got = MergeRules.pickPrimary(candidates)?.id
            val expected = if (c.isNull("expect")) null else c.getLong("expect")
            assertEquals(c.getString("name"), expected, got)
        }
    }

    @Test
    fun `dayOf 与契约一致`() {
        val cases = contract.getJSONArray("day_of")
        assertTrue(cases.length() > 0)
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            assertEquals(
                c.getString("created_at"),
                c.getString("expect"),
                MergeRules.dayOf(c.getString("created_at")),
            )
        }
    }

    @Test
    fun `rewind 与契约一致`() {
        val cases = contract.getJSONArray("rewind")
        assertTrue(cases.length() > 0)
        for (i in 0 until cases.length()) {
            val c = cases.getJSONObject(i)
            assertEquals(
                "rewind(${c.getDouble("watermark")})",
                c.getDouble("expect"),
                MergeRules.rewind(c.getDouble("watermark")),
                1e-9,
            )
        }
    }

    @Test
    fun `常量与契约一致`() {
        assertEquals(
            contract.getDouble("watermark_overlap_seconds"),
            MergeRules.WATERMARK_OVERLAP_SECONDS, 1e-9,
        )
        assertEquals(contract.getInt("plan_limit"), MergeRules.PLAN_LIMIT)
        assertEquals(contract.getInt("push_chunk"), MergeRules.PUSH_CHUNK)
        assertEquals(contract.getInt("max_pull_rounds"), MergeRules.MAX_PULL_ROUNDS)
    }

    @Test
    fun `重复应用同一批来料结果不变`() {
        // 「回退 300 秒重扫」能成立的前提：重复同步幂等。
        val rows = mutableMapOf<String, Double>()
        val incoming = listOf("a" to 100.0, "b" to 200.0, "c" to 150.0)
        repeat(3) {
            for ((uuid, ts) in incoming) {
                if (MergeRules.decide(rows[uuid], ts) != MergeRules.KEEP) rows[uuid] = ts
            }
        }
        assertEquals(mapOf("a" to 100.0, "b" to 200.0, "c" to 150.0), rows)
    }
}
