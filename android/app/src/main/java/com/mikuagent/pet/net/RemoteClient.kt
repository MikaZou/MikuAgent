package com.mikuagent.pet.net

import android.os.Handler
import android.os.Looper
import android.util.Log
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 与 PC 端 `backend/remote_server.py` 对话的 WebSocket 客户端。
 *
 * **协议一个字都没改**（改协议要同时动 PC 端，没必要）：
 *
 *   上行： {"type":"chat",  "text":…, "image":<b64 jpeg 可选>, "session_id":…}
 *          {"type":"audio", "data":<b64 wav>, "session_id":…}
 *          {"type":"ping"}
 *   下行： ready / transcript / reply / speech / error / pong
 *
 * 为什么 WebSocket 放在原生而不是页面里：页面跑在 `https://appassets...` 上，
 * 从 https 页面开 `ws://` 属于 mixed content，浏览器内核会拦掉。原生 socket
 * 完全不受这条规则约束 —— 这也是选混合架构的主要动机之一。
 */
class RemoteClient(private val onEvent: (Event) -> Unit) {

    // sealed **interface** 的实现在 Kotlin 里不能写 `: Event()`——
    // 带括号是「调用构造函数」，接口没有构造函数。这是编译错误 #3 的原因。
    sealed interface Event {
        /** PC 端就绪，带上它当前的 provider 信息 */
        data class Ready(val provider: JSONObject) : Event

        /** 语音转写结果 */
        data class Transcript(val text: String) : Event

        /** 文字回复 */
        data class Reply(val text: String, val emotion: String) : Event

        /** 语音回复（base64 WAV，32kHz 单声道） */
        data class Speech(val wavBase64: String, val duration: Double) : Event

        data class Error(val message: String) : Event

        /** 连接状态变化，供界面显示 */
        data class Status(val state: State, val detail: String = "") : Event

        enum class State { CONNECTING, CONNECTED, DISCONNECTED, FAILED }
    }

    private val main = Handler(Looper.getMainLooper())
    private val client = OkHttpClient.Builder()
        .connectTimeout(8, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS)   // 长连接不设读超时
        .pingInterval(20, TimeUnit.SECONDS)
        .build()

    @Volatile
    private var socket: WebSocket? = null

    @Volatile
    var sessionId: String? = null
        private set

    private var currentUrl: String? = null
    private var retryCount = 0
    private var manuallyClosed = false

    private fun post(e: Event) = main.post { onEvent(e) }

    /** 连接。host 可以是 `192.168.1.5`，也可以是模拟器访问宿主机的 `10.0.2.2`。 */
    fun connect(host: String, port: Int) {
        manuallyClosed = false
        val url = "ws://$host:$port/ws"
        if (url == currentUrl && socket != null) return
        currentUrl = url
        post(Event.Status(Event.State.CONNECTING, url))

        val req = Request.Builder().url(url).build()
        socket = client.newWebSocket(req, object : WebSocketListener() {
            override fun onOpen(ws: WebSocket, response: Response) {
                retryCount = 0
                Log.i(TAG, "已连接 $url")
                post(Event.Status(Event.State.CONNECTED, url))
            }

            override fun onMessage(ws: WebSocket, text: String) {
                dispatch(text)
            }

            override fun onFailure(ws: WebSocket, t: Throwable, response: Response?) {
                Log.w(TAG, "连接失败: ${t.message}")
                socket = null
                post(Event.Status(Event.State.FAILED, t.message ?: "未知错误"))
                scheduleReconnect()
            }

            override fun onClosed(ws: WebSocket, code: Int, reason: String) {
                Log.i(TAG, "连接关闭 code=$code reason=$reason")
                socket = null
                post(Event.Status(Event.State.DISCONNECTED, reason))
                scheduleReconnect()
            }
        })
    }

    private fun scheduleReconnect() {
        if (manuallyClosed) return
        val host = currentUrl ?: return
        // 退避：2s, 4s, 8s… 上限 15s，避免 PC 没开时疯狂重连
        val delayMs = minOf(15_000L, 2_000L shl minOf(retryCount, 3))
        retryCount++
        Log.i(TAG, "${delayMs}ms 后重连（第 $retryCount 次）")
        main.postDelayed({
            if (!manuallyClosed) {
                currentUrl = null      // 允许用同一个 URL 重新连
                val m = Regex("ws://([^:]+):(\\d+)/ws").find(host)
                if (m != null) connect(m.groupValues[1], m.groupValues[2].toInt())
            }
        }, delayMs)
    }

    private fun dispatch(text: String) {
        val json = try {
            JSONObject(text)
        } catch (e: Exception) {
            Log.w(TAG, "收到非法 JSON: ${text.take(80)}")
            return
        }
        when (val type = json.optString("type")) {
            "ready" -> {
                json.optJSONObject("provider")?.let { post(Event.Ready(it)) }
                    ?: post(Event.Ready(JSONObject()))
            }
            "transcript" -> post(Event.Transcript(json.optString("text")))
            "reply" -> {
                json.optString("session_id").takeIf { it.isNotEmpty() }?.let { sessionId = it }
                post(Event.Reply(json.optString("text"), json.optString("emotion", "NORMAL")))
            }
            "speech" -> post(Event.Speech(json.optString("data"), json.optDouble("duration", 0.0)))
            "error" -> post(Event.Error(json.optString("message")))
            "pong" -> Unit
            else -> Log.w(TAG, "未知消息类型: $type")
        }
    }

    private fun send(payload: JSONObject): Boolean {
        val ws = socket
        if (ws == null) {
            post(Event.Error("还没连上 PC"))
            return false
        }
        return ws.send(payload.toString())
    }

    fun sendChat(text: String, imageBase64: String? = null) {
        if (text.isBlank() && imageBase64 == null) return
        val o = JSONObject().apply {
            put("type", "chat")
            put("text", text.ifBlank { "（看看这张图）" })
            sessionId?.let { put("session_id", it) }
            imageBase64?.let { put("image", it) }
        }
        send(o)
    }

    fun sendAudio(wavBase64: String) {
        val o = JSONObject().apply {
            put("type", "audio")
            put("data", wavBase64)
            sessionId?.let { put("session_id", it) }
        }
        send(o)
    }

    fun ping() = send(JSONObject().put("type", "ping"))

    fun disconnect() {
        manuallyClosed = true
        socket?.close(1000, "bye")
        socket = null
        client.dispatcher.executorService.shutdown()
    }

    val isConnected: Boolean get() = socket != null

    companion object {
        private const val TAG = "RemoteClient"
    }
}
