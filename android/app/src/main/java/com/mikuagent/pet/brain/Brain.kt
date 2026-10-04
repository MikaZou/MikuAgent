package com.mikuagent.pet.brain

import android.content.Context
import android.content.SharedPreferences
import android.util.Base64
import android.util.Log
import com.mikuagent.pet.memory.MemoryStore
import com.mikuagent.pet.net.RemoteClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/** 对话在哪儿跑。 */
enum class BrainMode(val id: String, val label: String) {
    /** 手机直连云端 API（默认）。脱开 PC 也能用。 */
    API("api", "只用手机（直连 API）"),

    /** 走 PC 的后端服务（原来的行为）。 */
    PC("pc", "只用 PC"),

    /** 有 Key 就直连；没配 Key 但配了 PC 就走 PC。 */
    AUTO("auto", "自动"),
    ;

    /**
     * 实际生效的通道。
     *
     * `AUTO` 在「刚装好、还没填 Key、但配过 PC」时会落到 PC —— 这样升级上来
     * 的用户不会一开 App 就看到演示模式，以为坏了。
     */
    fun resolve(config: BrainConfig, pcConfigured: Boolean): BrainMode = when (this) {
        PC -> PC
        API -> API
        AUTO -> if (config.hasDeepseekKey) API else if (pcConfigured) PC else API
    }

    companion object {
        const val KEY = "brain_mode"

        fun from(id: String?): BrainMode =
            entries.firstOrNull { it.id == id } ?: AUTO

        fun load(prefs: SharedPreferences): BrainMode = from(prefs.getString(KEY, null))

        fun save(prefs: SharedPreferences, mode: BrainMode) {
            prefs.edit().putString(KEY, mode.id).apply()
        }
    }
}

/** 一轮对话的结果。 */
sealed class Reply {
    data class Ok(val text: String, val emotion: String, val sessionId: Long) : Reply()
    data class Err(val message: String) : Reply()
}

/**
 * 对话通道的统一接口。
 *
 * **回调式而不是 suspend 返回值**：PC 那条路是 WebSocket 异步的 ——
 * 发出去之后就结束了，结果要等 `Event.Reply` 回来。让两条路走同一个形状，
 * 上层的页面与口型逻辑就不用关心后面是哪一端。
 */
interface Brain {
    val kind: String

    /**
     * 这一端会不会**自己下发语音**。
     *
     * PC 会（`speech` 事件），所以上层什么都不用做；手机端得自己合成再播。
     */
    val deliversSpeech: Boolean

    fun send(
        sessionId: Long?,
        text: String,
        imageJpeg: ByteArray?,
        onResult: (Reply) -> Unit,
    )

    fun cancel()
}

/**
 * 手机端自己的大脑：直连 DeepSeek + MiniMax，记忆写本机 SQLite。
 */
class LocalBrain(
    private val context: Context,
    private val memory: MemoryStore,
    private val scope: CoroutineScope,
    private val config: () -> BrainConfig,
) : Brain {

    override val kind: String = "local"

    /** 语音由手机自己合成 —— PC 不会给这一端下发 `speech`。 */
    override val deliversSpeech: Boolean = false

    private var job: Job? = null

    override fun send(
        sessionId: Long?,
        text: String,
        imageJpeg: ByteArray?,
        onResult: (Reply) -> Unit,
    ) {
        job?.cancel()
        job = scope.launch {
            val agent = Agent(context, memory, config())
            val result = withContext(Dispatchers.IO) {
                runCatching { agent.chat(sessionId, text, imageJpeg) }
            }
            onResult(
                result.fold(
                    onSuccess = { Reply.Ok(it.reply, it.emotion, it.sessionId) },
                    onFailure = { Reply.Err(agent.humanError(it)) },
                )
            )
        }
    }

    override fun cancel() {
        job?.cancel()
        job = null
    }
}

/**
 * 走 PC 后端（原来的路径）。这里**只负责发**，结果由 `RemoteClient.Event.Reply`
 * 回到 MainActivity。
 */
class PcBrain(private val remote: RemoteClient) : Brain {

    override val kind: String = "pc"

    /** PC 会在 `reply` 之后自己补一条 `speech`。 */
    override val deliversSpeech: Boolean = true

    override fun send(
        sessionId: Long?,
        text: String,
        imageJpeg: ByteArray?,
        onResult: (Reply) -> Unit,
    ) {
        val image = imageJpeg?.let { Base64.encodeToString(it, Base64.NO_WRAP) }
        remote.sendChat(text, image)
        // 结果通过 WS 事件回来，这里不用 onResult（上层用同一个 deliverReply 接）
    }

    override fun cancel() = Unit
}

/** 按当前模式挑一个通道。 */
object BrainFactory {
    private const val TAG = "BrainFactory"

    fun pick(
        mode: BrainMode,
        config: BrainConfig,
        pcConfigured: Boolean,
        context: Context,
        memory: MemoryStore,
        scope: CoroutineScope,
        remote: RemoteClient?,
    ): Brain {
        val resolved = mode.resolve(config, pcConfigured)
        Log.i(TAG, "对话通道：$mode → $resolved（有 DeepSeek Key=${config.hasDeepseekKey}，配了 PC=$pcConfigured）")
        return if (resolved == BrainMode.PC && remote != null) {
            PcBrain(remote)
        } else {
            LocalBrain(context, memory, scope, { config })
        }
    }
}
