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
        /** 页面填了 PC 地址与端口后发起连接（页面自己不碰网络） */
        fun onConnect(host: String, port: Int)
        /** @return 空串表示开始成功，否则是给用户看的错误信息 */
        fun onStartRecording(): String
        fun onStopRecording()
        fun onCancelRecording()
        fun onPing()
        /** 拍一张照片发给 Miku 看；@return 空串表示已开始（结果异步回传），否则是错误信息 */
        fun onTakePhoto(): String
        /** 手机端「视频对话」开关：说话时是否自动附一帧画面 */
        fun onSetVideoMode(on: Boolean)
        /**
         * 手机上换 Live2D 模型。
         *
         * **本地直接切**：手机就是「自己用哪个模型」的主人（PC 上已经没有这个设置项）。
         * 切完顺手告诉 PC 记一笔 —— 只影响 `data/model_prefs.json` 的 `phone`
         * 与老写法的 `/model/manifest`，PC 不会再广播回来。
         */
        fun onSetModel(id: String)
        /**
         * 设置面板要显示的原生侧状态（JSON 字符串）。
         *
         * 页面拿不到自己的 PC 地址/端口/贴图倍率 —— 那些只在原生侧（SharedPreferences、
         * AssetServer），所以由原生一次性打包给它，省得页面去猜。
         */
        fun onDeviceState(): String
        /**
         * 保存 API 配置（JSON）。字段名与 PC 的 `.env` 一致
         * （`DEEPSEEK_API_KEY` 等），这样 PC 的「复制 API 配置」能直接粘进来。
         */
        fun onSaveApiConfig(json: String): String
        /** 读取 API 配置（Key 只回脱敏后的形式，用于界面回显）。 */
        fun onApiConfig(): String
        /** 切换对话通道 api / pc / auto。 */
        fun onSetBrainMode(mode: String)
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
    fun connect(host: String, port: Int) = actions.onConnect(host.trim(), port)

    /** 设置面板打开时调用，拿原生侧的地址/端口/贴图倍率等。 */
    @JavascriptInterface
    fun deviceState(): String = actions.onDeviceState()

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
    fun setVideoMode(on: Boolean) = actions.onSetVideoMode(on)

    @JavascriptInterface
    fun setModel(id: String) = actions.onSetModel(id.trim())

    /** 保存 API 配置；返回实际写入的字段名（逗号分隔），失败返回 `ERR:...`。 */
    @JavascriptInterface
    fun saveApiConfig(json: String): String = actions.onSaveApiConfig(json)

    @JavascriptInterface
    fun apiConfig(): String = actions.onApiConfig()

    @JavascriptInterface
    fun setBrainMode(mode: String) = actions.onSetBrainMode(mode.trim())

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

    /**
     * 可用模型清单 + 当前选中的那个（PC 端下发）。
     *
     * 页面用它在状态栏显示模型名，并提供「点一下换模型」的入口 ——
     * 手机上能换，PC 控制台上也能换，两边是同一份选择。
     *
     * ⚠️ 清单必须用 [q] 包成**字符串**再传：直接塞 JSON 文本的话，
     * JS 收到的是一个真数组，页面里的 `JSON.parse()` 会把它变成
     * `"[object Object]"` 然后解析失败 —— 表现就是模型按钮永远不出现
     * （真机实测踩到，日志里是 `模型清单解析失败: Unexpected token 'o'`）。
     */
    fun onModels(json: String, current: String) =
        callJs("window.Miku && Miku.onModels && Miku.onModels(${q(json)}, ${q(current)})")

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
