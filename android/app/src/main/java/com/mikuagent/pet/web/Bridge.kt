package com.mikuagent.pet.web

import android.util.Log
import android.webkit.JavascriptInterface
import android.webkit.WebView

/**
 * 页面 ↔ 原生 的双向桥。
 *
 * 分工：
 *   JS → Kotlin：用户操作（发消息、按住说话、拍照、状态汇报）
 *   Kotlin → JS：渲染指令（显示回复、转写结果、口型值、连接状态）
 *
 * 为什么长这样：页面跑在 `https://appassets...`，从 https 页面自己开 `ws://`
 * 会被 mixed content 规则拦掉；所以网络、录音、播放全部留在原生，
 * 页面只管把模型画出来。见 docs/ANDROID_PLAN.md §4.1。
 *
 * 线程约定：所有 `@JavascriptInterface` 方法都在 **WebView 的 JavaBridge 线程**
 * 被调用，所以实现里不要直接碰 UI；Kotlin→JS 的 [callJs] 可以任意线程调用，
 * 内部会 post 到 WebView 线程。
 */
class Bridge(
    private val web: WebView,
    private val actions: Actions,
) {

    /** 由 MainActivity 实现，把桥上的调用接到真正的原生逻辑。 */
    interface Actions {
        fun onChat(text: String, imageBase64: String?)
        /** 页面填了 PC 地址后发起连接（页面自己不碰网络） */
        fun onConnect(host: String)
        /** @return 空串表示开始成功，否则是给用户看的错误信息 */
        fun onStartRecording(): String
        fun onStopRecording()
        fun onCancelRecording()
        fun onPing()
        /** 拍一张照片发给 Miku 看；@return 空串表示已开始（结果异步回传），否则是错误信息 */
        fun onTakePhoto(): String
        /** 页面脚本**初始化完成**（与「模型渲染成功」是两件事） */
        fun onPageAlive()
        /** 页面自己也需要知道连接状态时用 */
        fun onPageReady(info: String)
        fun onPageFailed(reason: String)
    }

    // ------------------------------------------------------------ JS → Kotlin

    /**
     * 页面脚本已就绪，可以接收指令了。
     *
     * 必须和 [ready] 分开：`ready` 是「模型渲染成功」，而这里只是「JS 起来了」。
     * 早先只用一个信号，导致死锁 —— 原生等 pageReady 才调 loadModel()，
     * 页面又要等模型加载成功才调 ready()，两边互等，模型永远不加载。
     */
    @JavascriptInterface
    fun pageAlive() = actions.onPageAlive()

    @JavascriptInterface
    fun chat(text: String) = actions.onChat(text, null)

    @JavascriptInterface
    fun connect(host: String) = actions.onConnect(host.trim())

    @JavascriptInterface
    fun chatWithImage(text: String, imageBase64: String) =
        actions.onChat(text, imageBase64.ifBlank { null })

    @JavascriptInterface
    fun startRecording(): String {
        val err = actions.onStartRecording()
        if (err.isNotEmpty()) Log.w(TAG, "开始录音失败: $err")
        return err
    }

    @JavascriptInterface
    fun stopRecording() = actions.onStopRecording()

    @JavascriptInterface
    fun cancelRecording() = actions.onCancelRecording()

    @JavascriptInterface
    fun ping() = actions.onPing()

    @JavascriptInterface
    fun takePhoto(): String {
        val err = actions.onTakePhoto()
        if (err.isNotEmpty()) Log.w(TAG, "拍照失败: $err")
        return err
    }

    @JavascriptInterface
    fun ready(info: String) {
        Log.i(TAG, "RENDER_READY $info")
        actions.onPageReady(info)
    }

    @JavascriptInterface
    fun failed(reason: String) {
        Log.e(TAG, "RENDER_FAILED $reason")
        actions.onPageFailed(reason)
    }

    @JavascriptInterface
    fun log(msg: String) = Log.i("MikuJS", "[bridge] $msg")

    // ------------------------------------------------------------ Kotlin → JS

    fun onReply(text: String, emotion: String) =
        callJs("window.Miku && Miku.onReply(${q(text)}, ${q(emotion)})")

    fun onTranscript(text: String) =
        callJs("window.Miku && Miku.onTranscript(${q(text)})")

    fun onSpeech(durationSeconds: Double) =
        callJs("window.Miku && Miku.onSpeech($durationSeconds)")

    fun onStatus(state: String, detail: String) =
        callJs("window.Miku && Miku.onStatus(${q(state)}, ${q(detail)})")

    fun onProvider(json: String) =
        callJs("window.Miku && Miku.onProvider($json)")

    /** 语音播完了，让页面把状态收回「已就绪」。 */
    fun onSpeechEnd() = callJs("window.Miku && Miku.onSpeechEnd && Miku.onSpeechEnd()")

    fun onError(message: String) =
        callJs("window.Miku && Miku.onError(${q(message)})")

    /** 照片拍好了（base64 JPEG，不含 data: 前缀），交给页面挂在待发送的图片上。 */
    fun onPhoto(base64Jpeg: String) =
        callJs("window.Miku && Miku.onPhoto(${q(base64Jpeg)})")

    /**
     * 让页面开始加载模型。
     *
     * 由原生在**模型同步完成之后**调用 —— 顺序反了页面就会渲染失败，
     * 而且失败原因（文件缺失）会离真正的原因（还没同步）很远。
     */
    fun loadModel() = callJs("window.Miku && Miku.loadModel()")

    /** 口型值 0..1，播放语音时约 30Hz 调用。 */
    fun onMouth(level: Float) =
        callJs("window.Miku && Miku.onMouth($level)")

    /**
     * 执行一段 JS。任意线程可调。
     *
     * 用 `evaluateJavascript` 而不是 `loadUrl("javascript:...")`：后者会
     * 污染历史栈且没有回调，前者是官方推荐做法。
     */
    private fun callJs(script: String) {
        web.post {
            try {
                web.evaluateJavascript(script, null)
            } catch (e: Exception) {
                Log.w(TAG, "执行 JS 失败: ${e.message}")
            }
        }
    }

    /** 把字符串安全地包成 JS 字面量（转义引号、换行、反斜杠）。 */
    private fun q(s: String): String {
        val sb = StringBuilder(s.length + 2)
        sb.append('"')
        for (c in s) {
            when (c) {
                '\\' -> sb.append("\\\\")
                '"' -> sb.append("\\\"")
                '\n' -> sb.append("\\n")
                '\r' -> sb.append("\\r")
                '\t' -> sb.append("\\t")
                '\u2028' -> sb.append("\\u2028")
                '\u2029' -> sb.append("\\u2029")
                else -> if (c < ' ') sb.append("\\u%04x".format(c.code)) else sb.append(c)
            }
        }
        sb.append('"')
        return sb.toString()
    }

    companion object {
        private const val TAG = "MikuBridge"
    }
}
