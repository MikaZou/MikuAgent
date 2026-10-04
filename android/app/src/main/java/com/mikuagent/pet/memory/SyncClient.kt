package com.mikuagent.pet.memory

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 双端记忆同步（手机侧）。协议与合并规则见 `shared/sync_rules.json`
 * 与 `docs/TECHNICAL.md` §5.12。
 *
 * 手机是**发起方**：它才知道自己什么时候能连上（刚启动、网络恢复、聊完一轮）。
 * PC 只提供「给变动」与「收变动」两个动作，不带游标状态 —— 少一份状态就少一类
 * 「两端游标不一致」的故障。
 *
 * 两个游标都存在手机上：
 * * **拉取游标**用服务端返回的 `now`（不是本机时钟）—— 手机慢几秒就会漏行
 * * **推送游标**只能用自己的钟（PC 不记），所以每次回退 300 秒重扫
 *
 * 两者都靠 [MergeRules] 的幂等判定兜底：多扫一点只是多传几行，不会产生重复。
 */
class SyncClient(private val store: MemoryStore) {

    sealed class Result {
        /** @param pulled 拉取并落地的行数；@param pushed 推送的行数；@param created 新建的会话天数 */
        data class Ok(val pulled: Int, val pushed: Int, val created: Int) : Result()

        /** 连不上 / 服务端报错。同步失败**不影响**本地使用，只是这次没同步。 */
        data class Failed(val message: String) : Result()
    }

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(6, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(30, TimeUnit.SECONDS)
        .build()

    /**
     * 完整同步：先拉后推。
     *
     * 先拉再推是有意的：拉完之后本地就有了对方的最新版本，接着推自己的变更时，
     * 那些「对方更新、自己更旧」的行已经被覆盖，不会被自己的旧版本推回去。
     */
    fun sync(host: String, port: Int): Result = try {
        var pulled = 0
        var created = 0

        // ---------- 拉 ----------
        var cursor = MergeRules.rewind(store.syncState().pullWatermark)
        var rounds = 0
        var serverNow = 0.0
        var drained = false
        while (true) {
            val page = fetchChanges(host, port, cursor)
            serverNow = page.now
            val applied = store.applyBundle(page.bundle)
            pulled += applied.touched
            created += applied.daysCreated
            rounds++

            if (!page.hasMore) {
                drained = true
                break
            }
            if (rounds >= MergeRules.MAX_PULL_ROUNDS) {
                Log.w(TAG, "拉取轮数达到上限 ${MergeRules.MAX_PULL_ROUNDS}，剩下的下次再拉")
                break
            }
            val advanced = page.bundle.maxUpdatedAt
            if (advanced <= cursor) {
                // 服务端说还有、但游标推不动了。再循环就是死转，直接停。
                Log.w(TAG, "has_more 但游标不再前进（$cursor → $advanced），停止本轮拉取")
                break
            }
            cursor = advanced
        }

        // ---------- 推 ----------
        val pushed = pushChanges(host, port)

        // 游标只在**真正拉空**时才推进到服务端时间。
        // 中途停下的（轮数上限/不前进）必须留在原地，否则剩下的变动会被永久跳过 ——
        // 这正是「回退 300 秒重扫」也救不回来的错误。
        val pullWatermark = if (drained) MergeRules.rewind(serverNow) else cursor
        store.saveSyncState(
            pullWatermark = pullWatermark,
            pushWatermark = MergeRules.rewind(MemoryStore.nowEpoch()),
            lastSyncAt = MemoryStore.nowEpoch(),
            lastPullCount = pulled,
            lastPushCount = pushed,
            lastError = "",
        )
        Log.i(TAG, "同步完成：拉 $pulled 条（新建 $created 天会话）/ 推 $pushed 条" +
            if (drained) "" else "（未拉空，游标留在 $pullWatermark）")
        Result.Ok(pulled, pushed, created)
    } catch (e: Exception) {
        val msg = humanError(e)
        Log.w(TAG, "同步失败：$msg")
        store.saveSyncState(lastError = msg, lastSyncAt = MemoryStore.nowEpoch())
        Result.Failed(msg)
    }

    /**
     * 只推不拉。聊完一轮之后用 —— 本地刚写的东西要尽快让对方看到，
     * 而自己不需要为此再拉一遍。
     */
    fun pushOnly(host: String, port: Int): Result = try {
        val pushed = pushChanges(host, port)
        store.saveSyncState(
            pushWatermark = MergeRules.rewind(MemoryStore.nowEpoch()),
            lastSyncAt = MemoryStore.nowEpoch(),
            lastPushCount = pushed,
            lastError = "",
        )
        Result.Ok(0, pushed, 0)
    } catch (e: Exception) {
        val msg = humanError(e)
        store.saveSyncState(lastError = msg)
        Result.Failed(msg)
    }

    /**
     * 告诉 PC「本机现在用哪个 Live2D 模型」。
     *
     * 放在这个类里是因为它同样是「跟 PC 的 HTTP 通信」，共用同一个客户端与超时；
     * 独立模式下手机不连 WebSocket，所以原来的 WS `set_model` 通道用不了。
     * 语义与 WS 那条一致：**只记录，不广播** —— 手机才是自己模型的主人。
     */
    fun reportActiveModel(host: String, port: Int, modelId: String): Boolean = try {
        post("http://$host:$port/model/active", JSONObject().put("id", modelId))
        Log.i(TAG, "已通过 HTTP 向 PC 报告模型 $modelId")
        true
    } catch (e: Exception) {
        Log.w(TAG, "报告模型失败（不影响本地切换）：${e.message}")
        false
    }

    // -------------------------------------------------------------- 内部

    private class Page(val now: Double, val hasMore: Boolean, val bundle: SyncBundle)

    private fun fetchChanges(host: String, port: Int, since: Double): Page {
        val url = "http://$host:$port/sync/changes?since=$since&limit=${MergeRules.PLAN_LIMIT}"
        val json = get(url)
        return Page(
            now = json.optDouble("now", 0.0),
            hasMore = json.optBoolean("has_more", false),
            bundle = bundleFromJson(json),
        )
    }

    /**
     * 推送本机产出的行。
     *
     * `originOnly = "phone"`：从 PC 同步过来的行 origin 是 "pc"，
     * 推回去纯属回声（PC 时钟比自己快时还会每次重发）。
     */
    private fun pushChanges(host: String, port: Int): Int {
        var since = MergeRules.rewind(store.syncState().pushWatermark)
        var total = 0
        var rounds = 0
        while (rounds < MergeRules.MAX_PULL_ROUNDS) {
            val bundle = store.changesSince(
                since, MergeRules.PUSH_CHUNK, originOnly = MemoryStore.ORIGIN,
            )
            if (bundle.isEmpty) break
            post("http://$host:$port/sync/changes", bundleToJson(bundle))
            total += bundle.messageCount + bundle.memories.size + bundle.meta.size
            rounds++
            val advanced = bundle.maxUpdatedAt
            if (advanced <= since) break
            since = advanced
            if (bundle.messageCount < MergeRules.PUSH_CHUNK) break
        }
        return total
    }

    private fun get(url: String): JSONObject {
        val request = Request.Builder().url(url).get().build()
        http.newCall(request).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code} ${text.take(160)}")
            return JSONObject(text)
        }
    }

    private fun post(url: String, body: JSONObject) {
        val request = Request.Builder()
            .url(url)
            .header("Content-Type", "application/json")
            .post(body.toString().toRequestBody(JSON_MEDIA))
            .build()
        http.newCall(request).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) throw RuntimeException("HTTP ${resp.code} ${text.take(160)}")
        }
    }

    private fun humanError(e: Throwable): String {
        val msg = e.message.orEmpty()
        return when {
            msg.contains("Failed to connect") || msg.contains("ECONNREFUSED") ->
                "连不上 PC（它没开？还是手机不在同一网络？）"
            msg.contains("UnknownHost") -> "找不到 PC 的地址"
            msg.contains("timeout", ignoreCase = true) -> "同步超时"
            else -> "同步失败：${msg.take(120)}"
        }
    }

    // ---------------------------------------------------------- JSON 映射

    companion object {
        private const val TAG = "SyncClient"
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        fun bundleToJson(bundle: SyncBundle): JSONObject = JSONObject().apply {
            put("days", JSONArray().apply {
                for (day in bundle.days) {
                    put(JSONObject().apply {
                        put("day", day.day)
                        put("title", day.title)
                        put("updated_at", day.updatedAt)
                        put("deleted", if (day.deleted) 1 else 0)
                        put("messages", JSONArray().apply {
                            for (m in day.messages) put(messageToJson(m))
                        })
                    })
                }
            })
            put("memories", JSONArray().apply {
                for (m in bundle.memories) put(memoryToJson(m))
            })
            put("meta", JSONObject().apply {
                for ((key, entry) in bundle.meta) {
                    put(key, JSONObject().apply {
                        put("value", entry.value)
                        put("updated_at", entry.updatedAt)
                        put("deleted", if (entry.deleted) 1 else 0)
                    })
                }
            })
        }

        fun bundleFromJson(json: JSONObject): SyncBundle {
            val days = mutableListOf<DayBundle>()
            json.optJSONArray("days")?.let { arr ->
                for (i in 0 until arr.length()) {
                    val d = arr.getJSONObject(i)
                    val msgs = mutableListOf<MessageRow>()
                    d.optJSONArray("messages")?.let { marr ->
                        for (k in 0 until marr.length()) msgs += messageFromJson(marr.getJSONObject(k))
                    }
                    days += DayBundle(
                        day = d.optString("day"),
                        title = d.optString("title"),
                        updatedAt = d.optDouble("updated_at", 0.0),
                        deleted = d.optInt("deleted", 0) != 0,
                        messages = msgs,
                    )
                }
            }

            val memories = mutableListOf<MemoryRow>()
            json.optJSONArray("memories")?.let { arr ->
                for (i in 0 until arr.length()) memories += memoryFromJson(arr.getJSONObject(i))
            }

            val meta = mutableMapOf<String, MetaRow>()
            json.optJSONObject("meta")?.let { obj ->
                for (key in obj.keys()) {
                    val e = obj.getJSONObject(key)
                    meta[key] = MetaRow(
                        value = e.optString("value"),
                        updatedAt = e.optDouble("updated_at", 0.0),
                        deleted = e.optInt("deleted", 0) != 0,
                    )
                }
            }
            return SyncBundle(days, memories, meta)
        }

        private fun messageToJson(m: MessageRow) = JSONObject().apply {
            put("uuid", m.uuid)
            put("role", m.role)
            put("content", m.content)
            put("emotion", m.emotion ?: JSONObject.NULL)
            put("created_at", m.createdAt)
            put("updated_at", m.updatedAt)
            put("deleted", if (m.deleted) 1 else 0)
            put("origin", m.origin)
        }

        private fun messageFromJson(o: JSONObject) = MessageRow(
            id = 0,
            uuid = o.optString("uuid"),
            sessionId = 0,                     // 由 applyBundle 按「天」决定
            role = o.optString("role"),
            content = o.optString("content"),
            emotion = if (o.isNull("emotion")) null else o.optString("emotion"),
            createdAt = o.optString("created_at"),
            updatedAt = o.optDouble("updated_at", 0.0),
            deleted = o.optInt("deleted", 0) != 0,
            origin = o.optString("origin").ifBlank { "pc" },
        )

        private fun memoryToJson(m: MemoryRow) = JSONObject().apply {
            put("uuid", m.uuid)
            put("content", m.content)
            put("category", m.category)
            put("importance", m.importance)
            put("source", m.source ?: JSONObject.NULL)
            put("created_at", m.createdAt)
            put("updated_at", m.updatedAt)
            put("deleted", if (m.deleted) 1 else 0)
            put("origin", m.origin)
        }

        private fun memoryFromJson(o: JSONObject) = MemoryRow(
            id = 0,
            uuid = o.optString("uuid"),
            content = o.optString("content"),
            category = o.optString("category").ifBlank { "其他" },
            importance = o.optInt("importance", 3),
            source = if (o.isNull("source")) null else o.optString("source"),
            createdAt = o.optString("created_at"),
            updatedAt = o.optDouble("updated_at", 0.0),
            deleted = o.optInt("deleted", 0) != 0,
            origin = o.optString("origin").ifBlank { "pc" },
        )
    }
}
