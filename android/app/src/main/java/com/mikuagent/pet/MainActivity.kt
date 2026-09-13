package com.mikuagent.pet

import android.Manifest
import android.annotation.SuppressLint
import android.content.SharedPreferences
import android.content.pm.PackageManager
import android.os.Build
import android.os.Bundle
import android.util.Log
import android.view.View
import android.view.WindowManager
import android.webkit.ConsoleMessage
import android.webkit.WebChromeClient
import android.webkit.WebResourceRequest
import android.webkit.WebResourceResponse
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import com.mikuagent.pet.audio.AudioCapture
import com.mikuagent.pet.audio.AudioPlayer
import com.mikuagent.pet.model.ModelSync
import com.mikuagent.pet.net.RemoteClient
import com.mikuagent.pet.web.AssetServer
import com.mikuagent.pet.web.Bridge
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

/**
 * 路线 A 的手机客户端。
 *
 * 分工（详见 docs/ANDROID_PLAN.md §4）：
 *   WebView  → 只渲染 Live2D，且**不访问网络**（页面与模型都由本地喂入）
 *   Kotlin   → WebSocket / 模型同步 / 录音 / 播放 / 口型包络 / 相机
 *
 * 浏览器直开 `http://<PC>:8765/` 走不通的根本原因是
 * **`http://192.168.x.x` 不是安全上下文**，`navigator.mediaDevices` 直接是
 * undefined，麦克风与摄像头全部不可用；而且旧页把 `connect()` 写在 Live2D
 * 初始化之后，渲染一失败连文字聊天都一起死。这里两个问题都不存在。
 */
class MainActivity : AppCompatActivity(), Bridge.Actions {

    private lateinit var webView: WebView
    private lateinit var assets: AssetServer
    private lateinit var bridge: Bridge
    private lateinit var remote: RemoteClient
    private lateinit var capture: AudioCapture
    private lateinit var player: AudioPlayer
    private lateinit var sync: ModelSync
    private lateinit var prefs: SharedPreferences

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    @Volatile
    private var pageReady = false

    @Volatile
    private var modelReady = false

    /** 待处理的录音权限请求，授权后自动继续 */
    private var pendingRecordAction: (() -> Unit)? = null

    private val recordPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val action = pendingRecordAction
            pendingRecordAction = null
            if (granted) action?.invoke() else bridge.onError("没有麦克风权限，去系统设置里给一下？")
        }

    @SuppressLint("SetJavaScriptEnabled")
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        prefs = getSharedPreferences("miku", MODE_PRIVATE)

        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.P) {
            window.attributes.layoutInDisplayCutoutMode =
                WindowManager.LayoutParams.LAYOUT_IN_DISPLAY_CUTOUT_MODE_SHORT_EDGES
        }

        assets = AssetServer(this)
        capture = AudioCapture()
        // 口型包络直接喂给页面；30Hz 的频率 evaluateJavascript 扛得住
        player = AudioPlayer { level -> if (pageReady) bridge.onMouth(level) }
        sync = ModelSync(this)
        remote = RemoteClient { event -> onRemoteEvent(event) }

        webView = WebView(this).apply {
            setBackgroundColor(android.graphics.Color.BLACK)
            settings.apply {
                javaScriptEnabled = true
                domStorageEnabled = true
                mediaPlaybackRequiresUserGesture = false
                allowFileAccess = false
                allowContentAccess = false
                cacheMode = WebSettings.LOAD_DEFAULT
                mixedContentMode = WebSettings.MIXED_CONTENT_ALWAYS_ALLOW
            }
            webChromeClient = object : WebChromeClient() {
                override fun onConsoleMessage(msg: ConsoleMessage): Boolean {
                    val line = "${msg.message()}  (${msg.sourceId()}:${msg.lineNumber()})"
                    when (msg.messageLevel()) {
                        ConsoleMessage.MessageLevel.ERROR -> Log.e("MikuJS", line)
                        ConsoleMessage.MessageLevel.WARNING -> Log.w("MikuJS", line)
                        else -> Log.i("MikuJS", line)
                    }
                    return true
                }
            }
            webViewClient = object : WebViewClient() {
                override fun shouldInterceptRequest(
                    view: WebView,
                    request: WebResourceRequest,
                ): WebResourceResponse? = assets.shouldInterceptRequest(request.url)

                override fun onPageFinished(view: WebView, url: String) {
                    Log.i(TAG, "页面加载完成: $url")
                }
            }
        }
        bridge = Bridge(webView, this)
        webView.addJavascriptInterface(bridge, "NativeBridge")

        WebView.setWebContentsDebuggingEnabled(true)

        setContentView(webView)
        hideSystemBars()

        Log.i(TAG, "入口 ${assets.indexUrl()}")
        Log.i(TAG, "模型目录 ${sync.modelDir().absolutePath}")
        webView.loadUrl(assets.indexUrl())

        // 恢复上次的 PC 地址；模拟器上直接给出宿主机地址，省掉手输
        val saved = prefs.getString(KEY_HOST, null)
        val host = saved ?: defaultHostForPlatform()
        if (host != null) {
            Log.i(TAG, "自动连接 PC 地址 $host（来源：${if (saved != null) "上次保存" else "模拟器默认"}）")
            startConnecting(host)
        } else {
            Log.i(TAG, "尚未配置 PC 地址，等用户在界面上填")
        }
    }

    /**
     * 模拟器访问宿主机固定走 `10.0.2.2`，这不是猜的 —— 是 Android 模拟器的
     * NAT 约定。真机没有这样的固定地址，只能让用户填 PC 的局域网 IP。
     */
    private fun defaultHostForPlatform(): String? {
        val fp = Build.FINGERPRINT ?: ""
        val model = Build.MODEL ?: ""
        val product = Build.PRODUCT ?: ""
        val isEmulator = fp.startsWith("generic") || fp.contains("emulator") ||
            fp.contains("vbox") || model.contains("Emulator") ||
            model.contains("Android SDK built for") || product.contains("sdk")
        return if (isEmulator) EMULATOR_HOST else null
    }

    // ------------------------------------------------------------ 连接与同步

    private fun startConnecting(host: String) {
        val port = prefs.getInt(KEY_PORT, DEFAULT_PORT)
        prefs.edit().putString(KEY_HOST, host).putInt(KEY_PORT, port).apply()
        remote.connect(host, port)
        syncModel(host, port)
    }

    /**
     * 同步模型 → 通知页面加载。
     *
     * 顺序很重要：页面拿不到完整模型就会渲染失败，所以必须**先同步完再让它加载**。
     * 这也顺带解决了「下载中断留下半截文件」的问题 —— ModelSync 会按 sha1 校验。
     */
    private fun syncModel(host: String, port: Int) {
        scope.launch {
            val result = withContext(Dispatchers.IO) {
                sync.sync(host, port) { p ->
                    bridge.onStatus("syncing", "模型 ${p.done}/${p.total}  ${p.percent}%")
                }
            }
            when (result) {
                is ModelSync.Result.Ready -> {
                    Log.i(TAG, "模型就绪：下载 ${result.downloaded} 个，跳过 ${result.skipped} 个")
                    modelReady = true
                    bridge.onStatus("model_ready", "模型已就绪")
                    if (pageReady) bridge.loadModel()
                }
                is ModelSync.Result.Failed -> {
                    Log.e(TAG, "模型同步失败: ${result.message}")
                    bridge.onError("模型同步失败：${result.message}")
                }
            }
        }
    }

    private fun onRemoteEvent(e: RemoteClient.Event) {
        when (e) {
            is RemoteClient.Event.Ready -> {
                bridge.onProvider(e.provider.toString())
                bridge.onStatus("connected", "已连接")
            }
            is RemoteClient.Event.Transcript -> bridge.onTranscript(e.text)
            is RemoteClient.Event.Reply -> bridge.onReply(e.text, e.emotion)
            is RemoteClient.Event.Speech -> {
                val seconds = player.play(e.wavBase64)
                bridge.onSpeech(seconds)
            }
            is RemoteClient.Event.Error -> bridge.onError(e.message)
            is RemoteClient.Event.Status -> {
                val state = when (e.state) {
                    RemoteClient.Event.State.CONNECTING -> "connecting"
                    RemoteClient.Event.State.CONNECTED -> "connected"
                    RemoteClient.Event.State.DISCONNECTED -> "disconnected"
                    RemoteClient.Event.State.FAILED -> "failed"
                }
                bridge.onStatus(state, e.detail)
            }
        }
    }

    // ------------------------------------------------------- Bridge.Actions

    override fun onChat(text: String, imageBase64: String?) {
        if (!modelReady) Log.i(TAG, "模型还没就绪，但聊天不受影响")
        remote.sendChat(text, imageBase64)
    }

    override fun onConnect(host: String) {
        if (host.isBlank()) return
        Log.i(TAG, "用户指定 PC 地址: $host")
        startConnecting(host)
    }

    override fun onStartRecording(): String {
        val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.RECORD_AUDIO) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            pendingRecordAction = { capture.start()?.let { bridge.onError(it) } }
            recordPermLauncher.launch(Manifest.permission.RECORD_AUDIO)
            return ""   // 授权回调里再真正开始
        }
        return capture.start() ?: ""
    }

    override fun onStopRecording() {
        val result = capture.stop()
        if (result == null) {
            bridge.onError("录得太短啦")
            return
        }
        val (b64, seconds) = result
        Log.i(TAG, "上传语音 ${"%.2f".format(seconds)}s")
        remote.sendAudio(b64)
    }

    override fun onCancelRecording() = capture.cancel()

    // 注意用块体而不是 `= remote.ping()`：ping() 返回 Boolean，
    // 表达式体会把返回类型推断成 Boolean，与接口声明的 Unit 不符。
    override fun onPing() {
        remote.ping()
    }

    override fun onPageReady(info: String) {
        pageReady = true
        // 页面随时可能因为重载而重新 ready，这里只要模型已经同步过就让它加载
        if (modelReady) bridge.loadModel()
    }

    override fun onPageFailed(reason: String) {
        Log.e(TAG, "页面渲染失败: $reason")
        // 显存不足是最常见的原因：降采样贴图后重试一次
        if (assets.textureScale > 0.6) {
            assets.textureScale = 0.5
            Log.w(TAG, "降低贴图精度到 ${assets.textureScale} 后重试")
            bridge.onStatus("retry", "显存不足，改用半尺寸贴图重试")
            webView.postDelayed({ webView.reload() }, 800)
        }
    }

    // ---------------------------------------------------------------- 杂项

    private fun hideSystemBars() {
        @Suppress("DEPRECATION")
        window.decorView.systemUiVisibility = (
            View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                or View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN
                or View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                or View.SYSTEM_UI_FLAG_FULLSCREEN
                or View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
            )
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
    }

    override fun onPause() {
        webView.onPause()
        super.onPause()
    }

    override fun onDestroy() {
        scope.cancel()
        capture.cancel()
        player.stop()
        remote.disconnect()
        webView.destroy()
        super.onDestroy()
    }

    companion object {
        private const val TAG = "MikuAgent"
        private const val KEY_HOST = "pc_host"
        private const val KEY_PORT = "pc_port"

        /**
         * 模拟器访问宿主机的固定地址；真机需要填 PC 的局域网 IP
         * （PC 端气泡里会显示，形如 192.168.20.102）。
         */
        const val DEFAULT_PORT = 8765
        const val EMULATOR_HOST = "10.0.2.2"
    }
}
