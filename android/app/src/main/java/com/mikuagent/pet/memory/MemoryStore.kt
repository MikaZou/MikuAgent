package com.mikuagent.pet.memory

import android.content.ContentValues
import android.content.Context
import android.database.Cursor
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import android.util.Log
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import java.util.UUID

/**
 * 手机端记忆库（SQLite）。对应 PC 的 `backend/memory.py`。
 *
 * 布局与 PC 完全同构，只多了同步需要的列（`uuid` / `updated_at` / `deleted` /
 * `origin`）。这么设计是为了让同步**不需要任何字段映射** —— 两端就是同一张表，
 * 按 `uuid` 认领、按 `updated_at` 比新旧。
 *
 * 会话规则也照抄 PC：**只由日期决定一条**（`preferredId` 刻意被忽略）。
 * 理由见 PC 那边 `resolve_session` 的注释：如果规则是「只要是今天的 id 就沿用」，
 * 手机和 PC 各自缓存着自己那一条，两端会永久分裂，跨设备接着聊就永远做不到。
 *
 * 线程：所有方法都会落到 `SQLiteOpenHelper` 的同一个连接上，SQLite 自己保证
 * 串行化；调用方仍然只在 `Dispatchers.IO` 上调（不阻塞主线程）。
 */
class MemoryStore private constructor(context: Context) :
    SQLiteOpenHelper(context.applicationContext, DB_NAME, null, DB_VERSION) {

    companion object {
        private const val TAG = "MemoryStore"
        private const val DB_NAME = "mikuagent.db"
        private const val DB_VERSION = 1

        /** 本机写入的行的来源标记。 */
        const val ORIGIN = "phone"

        /** 从 PC 同步过来的行的来源标记。 */
        const val PEER_ORIGIN = "pc"

        /** 会话按天划分：切分点是本地 00:00，凌晨聊天算第二天（与 PC 一致）。 */
        private val DAY_FMT = SimpleDateFormat("yyyy-MM-dd", Locale.US)
        private val STAMP_FMT = SimpleDateFormat("yyyy-MM-dd HH:mm:ss", Locale.US)

        @Volatile
        private var instance: MemoryStore? = null

        fun get(context: Context): MemoryStore =
            instance ?: synchronized(this) {
                instance ?: MemoryStore(context).also { instance = it }
            }

        /** 与 PC 的 `_now()` 同格式：本地时间字符串，秒精度。 */
        fun now(): String = STAMP_FMT.format(Date())

        fun today(): String = DAY_FMT.format(Date())

        /** epoch 秒（带毫秒）。LWW 与同步游标都用它。 */
        fun nowEpoch(): Double = System.currentTimeMillis() / 1000.0

        /** 与 PC 的 `lower(hex(randomblob(16)))` 同形状：32 位十六进制、无短横线。 */
        fun newUuid(): String = UUID.randomUUID().toString().replace("-", "")
    }

    override fun onCreate(db: SQLiteDatabase) {
        // 注意：Android 的 SQLiteDatabase **没有** `executescript`（那是 Python 的），
        // 只能一条条 execSQL。
        SCHEMA.forEach(db::execSQL)
    }

    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) {
        // 还没有历史版本。将来加列时在这里按 oldVersion 分支做 ALTER，
        // **不要** DROP —— 用户的聊天记录不能因为升级没了。
        Log.w(TAG, "onUpgrade $oldVersion -> $newVersion 没有迁移分支（不该发生）")
    }

    // ------------------------------------------------------------------ 会话

    /**
     * 今天的会话。**[这是唯一该用的入口]**。
     *
     * `preferredId` 故意不参与选择，只是为了与 PC `resolve_session` 签名一致 ——
     * 会话**只由日期决定**，客户端说不上话。
     */
    fun resolveSession(preferredId: Long? = null): SessionRow {
        primarySessionOfDay(today())?.let { return it }
        return createSession(today())
    }

    /** 该天「消息最多」的那条会话；同一天有多条时靠它收敛（规则见 [MergeRules]）。 */
    fun primarySessionOfDay(day: String): SessionRow? {
        val rows = sessionsOfDay(day)
        val primary = MergeRules.pickPrimary(
            rows.map { MergeRules.SessionRef(it.id, it.createdAt, messageCountOf(it.id)) }
        ) ?: return null
        return getSession(primary.id)
    }

    fun sessionsOfDay(day: String): List<SessionRow> =
        readable.rawQuery(
            "SELECT * FROM sessions WHERE substr(created_at,1,10) = ? AND deleted = 0",
            arrayOf(day),
        ).use { it.mapRows(::readSession) }

    fun getSession(id: Long): SessionRow? =
        readable.rawQuery("SELECT * FROM sessions WHERE id = ?", arrayOf(id.toString()))
            .use { if (it.moveToFirst()) readSession(it) else null }

    fun messageCountOf(sessionId: Long): Int =
        readable.rawQuery(
            "SELECT COUNT(*) FROM messages WHERE session_id = ? AND deleted = 0",
            arrayOf(sessionId.toString()),
        ).use { if (it.moveToFirst()) it.getInt(0) else 0 }

    fun createSession(
        title: String? = null,
        uuid: String = newUuid(),
        createdAt: String = now(),
        origin: String = ORIGIN,
    ): SessionRow {
        val id = writable.insert(
            "sessions", null,
            ContentValues().apply {
                put("uuid", uuid)
                put("title", title ?: "新的对话")
                put("created_at", createdAt)
                put("updated_at", nowEpoch())
                put("deleted", 0)
                put("origin", origin)
            },
        )
        return getSession(id)!!
    }

    fun renameSession(id: Long, title: String) {
        writable.update(
            "sessions",
            ContentValues().apply {
                put("title", title.trim().ifEmpty { "新的对话" })
                put("updated_at", nowEpoch())
            },
            "id = ?", arrayOf(id.toString()),
        )
    }

    fun listSessions(limit: Int = 50): List<SessionSummary> =
        readable.rawQuery(
            """
            SELECT s.id, s.title, s.created_at,
                   COUNT(m.id) AS message_count,
                   MAX(m.created_at) AS last_active
            FROM sessions s
            LEFT JOIN messages m ON m.session_id = s.id AND m.deleted = 0
            WHERE s.deleted = 0
            GROUP BY s.id
            ORDER BY COALESCE(last_active, s.created_at) DESC, s.id DESC
            LIMIT ?
            """.trimIndent(),
            arrayOf(limit.toString()),
        ).use { c ->
            (0 until c.count).map {
                c.moveToPosition(it)
                SessionSummary(
                    id = c.getLong(c.getColumnIndexOrThrow("id")),
                    title = c.getString(c.getColumnIndexOrThrow("title")),
                    createdAt = c.getString(c.getColumnIndexOrThrow("created_at")),
                    messageCount = c.getInt(c.getColumnIndexOrThrow("message_count")),
                    lastActive = c.getStringOrNull("last_active"),
                )
            }
        }

    // ------------------------------------------------------------------ 消息

    fun addMessage(
        sessionId: Long,
        role: String,
        content: String,
        emotion: String? = null,
        origin: String = ORIGIN,
    ): MessageRow {
        val ts = nowEpoch()
        return writable.transaction {
            val id = writable.insert(
                "messages", null,
                ContentValues().apply {
                    put("uuid", newUuid())
                    put("session_id", sessionId)
                    put("role", role)
                    put("content", content)
                    put("emotion", emotion)
                    put("created_at", now())
                    put("updated_at", ts)
                    put("deleted", 0)
                    put("origin", origin)
                },
            )
            // 会话自身的 updated_at 也要跟上，否则同步「按变动取天」会漏掉这一天
            writable.execSQL(
                "UPDATE sessions SET updated_at = ? WHERE id = ? AND updated_at < ?",
                arrayOf(ts, sessionId, ts),
            )
            getMessage(id)!!
        }
    }

    fun getMessage(id: Long): MessageRow? =
        readable.rawQuery("SELECT * FROM messages WHERE id = ?", arrayOf(id.toString()))
            .use { if (it.moveToFirst()) readMessage(it) else null }

    /** 取会话历史（时间正序）。`limit` 取的是**最近 N 条**，与 PC 一致。 */
    fun getMessages(sessionId: Long, limit: Int? = null): List<MessageRow> =
        if (limit != null) {
            readable.rawQuery(
                """
                SELECT * FROM (
                    SELECT * FROM messages
                    WHERE session_id = ? AND deleted = 0
                    ORDER BY id DESC LIMIT ?
                ) ORDER BY id
                """.trimIndent(),
                arrayOf(sessionId.toString(), limit.toString()),
            ).use { it.mapRows(::readMessage) }
        } else {
            readable.rawQuery(
                "SELECT * FROM messages WHERE session_id = ? AND deleted = 0 ORDER BY id",
                arrayOf(sessionId.toString()),
            ).use { it.mapRows(::readMessage) }
        }

    // -------------------------------------------------------------- 长期记忆

    fun addMemory(
        content: String,
        category: String = "其他",
        importance: Int = 3,
        source: String = "对话",
        origin: String = ORIGIN,
    ): MemoryRow {
        val text = content.trim()
        require(text.isNotEmpty()) { "记忆内容不能为空" }
        val ts = nowEpoch()
        val id = writable.insert(
            "memory_items", null,
            ContentValues().apply {
                put("uuid", newUuid())
                put("content", text)
                put("category", category)
                put("importance", importance)
                put("source", source)
                put("created_at", now())
                put("updated_at", ts)
                put("deleted", 0)
                put("origin", origin)
            },
        )
        return getMemory(id)!!
    }

    fun getMemory(id: Long): MemoryRow? =
        readable.rawQuery("SELECT * FROM memory_items WHERE id = ?", arrayOf(id.toString()))
            .use { if (it.moveToFirst()) readMemory(it) else null }

    /** 按重要度倒序、其次按新近（与 PC `list_memories` 一致）。 */
    fun listMemories(limit: Int = 50): List<MemoryRow> =
        readable.rawQuery(
            """
            SELECT * FROM memory_items WHERE deleted = 0
            ORDER BY importance DESC, id DESC LIMIT ?
            """.trimIndent(),
            arrayOf(limit.toString()),
        ).use { it.mapRows(::readMemory) }

    fun deleteMemory(id: Long): Boolean =
        writable.update(
            "memory_items",
            ContentValues().apply {
                put("deleted", 1)
                put("updated_at", nowEpoch())
            },
            "id = ?", arrayOf(id.toString()),
        ) > 0

    // ---------------------------------------------------------------- 元数据

    fun getMeta(key: String, default: String = ""): String =
        readable.rawQuery(
            "SELECT value FROM meta WHERE key = ? AND deleted = 0", arrayOf(key),
        ).use { if (it.moveToFirst()) it.getString(0) else default }

    fun setMeta(key: String, value: String) {
        writable.insertWithOnConflict(
            "meta", null,
            ContentValues().apply {
                put("key", key)
                put("value", value)
                put("updated_at", nowEpoch())
                put("deleted", 0)
            },
            SQLiteDatabase.CONFLICT_REPLACE,
        )
    }

    // ------------------------------------------------------------------ 同步

    /**
     * 取出 `updated_at > since` 的变动。
     *
     * **单位是「天」**：同一批变动消息按日期归组，连这一天的会话信息一起打包。
     * 这样接收端不需要知道发送端的会话 id —— 它按日期找自己那一条就行
     * （会话本来就「一天一条」）。
     */
    fun changesSince(since: Double, limit: Int = MergeRules.PLAN_LIMIT): SyncBundle {
        val changed = readable.rawQuery(
            "SELECT * FROM messages WHERE updated_at > ? ORDER BY updated_at LIMIT ?",
            arrayOf(since.toString(), limit.toString()),
        ).use { it.mapRows(::readMessage) }

        val days = changed.groupBy { MergeRules.dayOf(it.createdAt) }
            .filterKeys { it.isNotBlank() }
            .map { (day, msgs) ->
                val session = primarySessionOfDay(day)
                DayBundle(
                    day = day,
                    title = session?.title ?: day,
                    updatedAt = msgs.maxOf { it.updatedAt },
                    deleted = false,
                    messages = msgs,
                )
            }

        val memories = readable.rawQuery(
            "SELECT * FROM memory_items WHERE updated_at > ? ORDER BY updated_at LIMIT ?",
            arrayOf(since.toString(), limit.toString()),
        ).use { it.mapRows(::readMemory) }

        val meta = readable.rawQuery(
            "SELECT * FROM meta WHERE updated_at > ?", arrayOf(since.toString()),
        ).use { c ->
            (0 until c.count).associate {
                c.moveToPosition(it)
                val key = c.getString(c.getColumnIndexOrThrow("key"))
                key to MetaRow(
                    value = c.getString(c.getColumnIndexOrThrow("value")),
                    updatedAt = c.getDouble(c.getColumnIndexOrThrow("updated_at")),
                    deleted = c.getInt(c.getColumnIndexOrThrow("deleted")) != 0,
                )
            }
        }

        return SyncBundle(days = days, memories = memories, meta = meta)
    }

    /**
     * 把对方的一批变动合并进来。整体一个事务 —— 中途失败不能留下半套数据，
     * 否则下次同步的游标就对不上了。
     *
     * 冲突判定一律走 [MergeRules.decide]（契约在 `shared/sync_rules.json`）。
     */
    fun applyBundle(bundle: SyncBundle): ApplyResult {
        var inserted = 0
        var updated = 0
        var kept = 0
        var daysCreated = 0

        writable.transaction {
            for (day in bundle.days) {
                if (day.day.isBlank()) continue
                var session = primarySessionOfDay(day.day)
                if (session == null) {
                    session = createSession(
                        title = day.title.ifBlank { day.day },
                        createdAt = "${day.day} 00:00:00",
                        origin = PEER_ORIGIN,
                    )
                    daysCreated++
                }

                for (msg in day.messages) {
                    val local = messageByUuid(msg.uuid)
                    when (MergeRules.decide(local?.updatedAt, msg.updatedAt)) {
                        MergeRules.INSERT -> {
                            writable.insert(
                                "messages", null,
                                ContentValues().apply {
                                    put("uuid", msg.uuid)
                                    put("session_id", session.id)
                                    put("role", msg.role)
                                    put("content", msg.content)
                                    put("emotion", msg.emotion)
                                    put("created_at", msg.createdAt)
                                    put("updated_at", msg.updatedAt)
                                    put("deleted", if (msg.deleted) 1 else 0)
                                    put("origin", msg.origin)
                                },
                            )
                            inserted++
                        }
                        MergeRules.UPDATE -> {
                            writable.update(
                                "messages",
                                ContentValues().apply {
                                    put("session_id", session.id)
                                    put("role", msg.role)
                                    put("content", msg.content)
                                    put("emotion", msg.emotion)
                                    put("created_at", msg.createdAt)
                                    put("updated_at", msg.updatedAt)
                                    put("deleted", if (msg.deleted) 1 else 0)
                                },
                                "uuid = ?", arrayOf(msg.uuid),
                            )
                            updated++
                        }
                        else -> kept++
                    }
                }
                // 会话自身的标题/时间戳也按 LWW 走一次；
                // 本地刚聊过的话本地更新，这里就不会动它
                if (MergeRules.decide(session.updatedAt, day.updatedAt) == MergeRules.UPDATE) {
                    writable.update(
                        "sessions",
                        ContentValues().apply {
                            put("title", day.title.ifBlank { day.day })
                            put("updated_at", day.updatedAt)
                        },
                        "id = ?", arrayOf(session.id.toString()),
                    )
                }
            }

            for (mem in bundle.memories) {
                val local = memoryByUuid(mem.uuid)
                when (MergeRules.decide(local?.updatedAt, mem.updatedAt)) {
                    MergeRules.INSERT -> {
                        writable.insert(
                            "memory_items", null,
                            ContentValues().apply {
                                put("uuid", mem.uuid)
                                put("content", mem.content)
                                put("category", mem.category)
                                put("importance", mem.importance)
                                put("source", mem.source)
                                put("created_at", mem.createdAt)
                                put("updated_at", mem.updatedAt)
                                put("deleted", if (mem.deleted) 1 else 0)
                                put("origin", mem.origin)
                            },
                        )
                        inserted++
                    }
                    MergeRules.UPDATE -> {
                        writable.update(
                            "memory_items",
                            ContentValues().apply {
                                put("content", mem.content)
                                put("category", mem.category)
                                put("importance", mem.importance)
                                put("source", mem.source)
                                put("updated_at", mem.updatedAt)
                                put("deleted", if (mem.deleted) 1 else 0)
                            },
                            "uuid = ?", arrayOf(mem.uuid),
                        )
                        updated++
                    }
                    else -> kept++
                }
            }

            for ((key, entry) in bundle.meta) {
                val local = metaUpdatedAt(key)
                when (MergeRules.decide(local, entry.updatedAt)) {
                    MergeRules.INSERT, MergeRules.UPDATE -> {
                        writable.insertWithOnConflict(
                            "meta", null,
                            ContentValues().apply {
                                put("key", key)
                                put("value", entry.value)
                                put("updated_at", entry.updatedAt)
                                put("deleted", if (entry.deleted) 1 else 0)
                            },
                            SQLiteDatabase.CONFLICT_REPLACE,
                        )
                        if (local == null) inserted++ else updated++
                    }
                    else -> kept++
                }
            }
        }

        return ApplyResult(inserted, updated, kept, daysCreated)
    }

    fun messageByUuid(uuid: String): MessageRow? =
        readable.rawQuery("SELECT * FROM messages WHERE uuid = ?", arrayOf(uuid))
            .use { if (it.moveToFirst()) readMessage(it) else null }

    fun memoryByUuid(uuid: String): MemoryRow? =
        readable.rawQuery("SELECT * FROM memory_items WHERE uuid = ?", arrayOf(uuid))
            .use { if (it.moveToFirst()) readMemory(it) else null }

    fun metaUpdatedAt(key: String): Double? =
        readable.rawQuery("SELECT updated_at FROM meta WHERE key = ?", arrayOf(key))
            .use { if (it.moveToFirst()) it.getDouble(0) else null }

    fun syncState(): SyncState =
        readable.rawQuery("SELECT * FROM sync_state WHERE id = 1", null).use { c ->
            if (!c.moveToFirst()) {
                SyncState.EMPTY
            } else {
                SyncState(
                    pullWatermark = c.getDouble(c.getColumnIndexOrThrow("pull_watermark")),
                    pushWatermark = c.getDouble(c.getColumnIndexOrThrow("push_watermark")),
                    lastSyncAt = c.getDouble(c.getColumnIndexOrThrow("last_sync_at")),
                    lastPullCount = c.getInt(c.getColumnIndexOrThrow("last_pull_count")),
                    lastPushCount = c.getInt(c.getColumnIndexOrThrow("last_push_count")),
                    lastError = c.getString(c.getColumnIndexOrThrow("last_error")),
                )
            }
        }

    fun saveSyncState(
        pullWatermark: Double? = null,
        pushWatermark: Double? = null,
        lastSyncAt: Double? = null,
        lastPullCount: Int? = null,
        lastPushCount: Int? = null,
        lastError: String? = null,
    ) {
        val current = syncState()
        writable.insertWithOnConflict(
            "sync_state", null,
            ContentValues().apply {
                put("id", 1)
                put("pull_watermark", pullWatermark ?: current.pullWatermark)
                put("push_watermark", pushWatermark ?: current.pushWatermark)
                put("last_sync_at", lastSyncAt ?: current.lastSyncAt)
                put("last_pull_count", lastPullCount ?: current.lastPullCount)
                put("last_push_count", lastPushCount ?: current.lastPushCount)
                put("last_error", lastError ?: current.lastError)
            },
            SQLiteDatabase.CONFLICT_REPLACE,
        )
    }

    /** 清空同步游标（换了 PC / 想强制全量重来一次时用）。 */
    fun resetSyncWatermarks() {
        saveSyncState(pullWatermark = 0.0, pushWatermark = 0.0, lastError = "")
    }

    /** 排查用：各表行数。设置面板与日志都会用到。 */
    fun counts(): Map<String, Int> = mapOf(
        "sessions" to count("SELECT COUNT(*) FROM sessions WHERE deleted = 0"),
        "messages" to count("SELECT COUNT(*) FROM messages WHERE deleted = 0"),
        "memories" to count("SELECT COUNT(*) FROM memory_items WHERE deleted = 0"),
        "tombstones" to (
            count("SELECT COUNT(*) FROM messages WHERE deleted = 1") +
                count("SELECT COUNT(*) FROM memory_items WHERE deleted = 1")
            ),
    )

    private fun count(sql: String): Int =
        readable.rawQuery(sql, null).use { if (it.moveToFirst()) it.getInt(0) else 0 }

    // ------------------------------------------------------------- 内部小工具

    private val readable: SQLiteDatabase get() = readableDatabase
    private val writable: SQLiteDatabase get() = writableDatabase

    private fun <T> SQLiteDatabase.transaction(body: () -> T): T {
        beginTransaction()
        return try {
            val out = body()
            setTransactionSuccessful()
            out
        } finally {
            endTransaction()
        }
    }

    private fun <T> Cursor.mapRows(read: (Cursor) -> T): List<T> =
        (0 until count).map {
            moveToPosition(it)
            read(this)
        }

    private fun readSession(c: Cursor) = SessionRow(
        id = c.getLong(c.getColumnIndexOrThrow("id")),
        uuid = c.getString(c.getColumnIndexOrThrow("uuid")),
        title = c.getString(c.getColumnIndexOrThrow("title")),
        createdAt = c.getString(c.getColumnIndexOrThrow("created_at")),
        updatedAt = c.getDouble(c.getColumnIndexOrThrow("updated_at")),
        deleted = c.getInt(c.getColumnIndexOrThrow("deleted")) != 0,
        origin = c.getString(c.getColumnIndexOrThrow("origin")),
    )

    private fun readMessage(c: Cursor) = MessageRow(
        id = c.getLong(c.getColumnIndexOrThrow("id")),
        uuid = c.getString(c.getColumnIndexOrThrow("uuid")),
        sessionId = c.getLong(c.getColumnIndexOrThrow("session_id")),
        role = c.getString(c.getColumnIndexOrThrow("role")),
        content = c.getString(c.getColumnIndexOrThrow("content")),
        emotion = c.getStringOrNull("emotion"),
        createdAt = c.getString(c.getColumnIndexOrThrow("created_at")),
        updatedAt = c.getDouble(c.getColumnIndexOrThrow("updated_at")),
        deleted = c.getInt(c.getColumnIndexOrThrow("deleted")) != 0,
        origin = c.getString(c.getColumnIndexOrThrow("origin")),
    )

    private fun readMemory(c: Cursor) = MemoryRow(
        id = c.getLong(c.getColumnIndexOrThrow("id")),
        uuid = c.getString(c.getColumnIndexOrThrow("uuid")),
        content = c.getString(c.getColumnIndexOrThrow("content")),
        category = c.getString(c.getColumnIndexOrThrow("category")),
        importance = c.getInt(c.getColumnIndexOrThrow("importance")),
        source = c.getStringOrNull("source"),
        createdAt = c.getString(c.getColumnIndexOrThrow("created_at")),
        updatedAt = c.getDouble(c.getColumnIndexOrThrow("updated_at")),
        deleted = c.getInt(c.getColumnIndexOrThrow("deleted")) != 0,
        origin = c.getString(c.getColumnIndexOrThrow("origin")),
    )

    private fun Cursor.getStringOrNull(name: String): String? {
        val i = getColumnIndexOrThrow(name)
        return if (isNull(i)) null else getString(i)
    }

    private val SCHEMA = listOf(
        """
        CREATE TABLE sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL DEFAULT '新的对话',
            created_at TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT ''
        )
        """.trimIndent(),
        """
        CREATE TABLE messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            session_id INTEGER NOT NULL,
            role TEXT NOT NULL,
            content TEXT NOT NULL,
            emotion TEXT,
            created_at TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT ''
        )
        """.trimIndent(),
        """
        CREATE TABLE memory_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            uuid TEXT NOT NULL UNIQUE,
            content TEXT NOT NULL,
            category TEXT NOT NULL DEFAULT '其他',
            importance INTEGER NOT NULL DEFAULT 3,
            source TEXT,
            created_at TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0,
            origin TEXT NOT NULL DEFAULT ''
        )
        """.trimIndent(),
        """
        CREATE TABLE meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at REAL NOT NULL DEFAULT 0,
            deleted INTEGER NOT NULL DEFAULT 0
        )
        """.trimIndent(),
        """
        CREATE TABLE sync_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            pull_watermark REAL NOT NULL DEFAULT 0,
            push_watermark REAL NOT NULL DEFAULT 0,
            last_sync_at REAL NOT NULL DEFAULT 0,
            last_pull_count INTEGER NOT NULL DEFAULT 0,
            last_push_count INTEGER NOT NULL DEFAULT 0,
            last_error TEXT NOT NULL DEFAULT ''
        )
        """.trimIndent(),
        "INSERT INTO sync_state (id) VALUES (1)",
        "CREATE INDEX idx_messages_session ON messages(session_id)",
        "CREATE INDEX idx_messages_updated ON messages(updated_at)",
        "CREATE INDEX idx_sessions_created ON sessions(created_at)",
        "CREATE INDEX idx_memories_updated ON memory_items(updated_at)",
    )
}
