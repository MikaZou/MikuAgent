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
import com.mikuagent.pet.camera.PhotoTaker
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
    private lateinit var photoTaker: PhotoTaker

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    @Volatile
    private var pageReady = false

    @Volatile
    private var modelReady = false

    /** 最近一次连接状态。连接可能早于页面就绪，那时要在 onPageAlive 里补发。 */
    @Volatile
    private var lastStatus: Pair<String, String>? = null

    /** 模型同步的单飞锁：防止 onCreate 与 WS 就绪两处并发触发同一次同步。 */
    private val syncing = java.util.concurrent.atomic.AtomicBoolean(false)

    /** 待处理的录音权限请求，授权后自动继续 */
    private var pendingRecordAction: (() -> Unit)? = null

    /** 待处理的相机权限请求 */
    private var pendingPhotoAction: (() -> Unit)? = null

    private val recordPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val action = pendingRecordAction
            pendingRecordAction = null
            if (granted) action?.invoke() else bridge.onError("没有麦克风权限，去系统设置里给一下？")
        }

    private val cameraPermLauncher =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { granted ->
            val action = pendingPhotoAction
            pendingPhotoAction = null
            if (granted) action?.invoke() else bridge.onError("没有相机权限，去系统设置里给一下？")
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
        photoTaker = PhotoTaker(this, this)
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
     *
     * **单飞保护**：本方法有两个触发点（onCreate 的首次连接、以及 WS 就绪后的补同步），
     * 真机上实测会**并发跑两次**，两个线程往同一个临时文件下载，一个搬走后另一个
     * 就「落盘失败」，导致整个同步中断、模型缺文件。模拟器上时序错开没撞上。
     */
    private fun syncModel(host: String, port: Int) {
        if (!syncing.compareAndSet(false, true)) {
            Log.i(TAG, "已有同步在进行，跳过本次触发")
            return
        }
        scope.launch {
            try {
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
            } finally {
                syncing.set(false)
            }
        }
    }

    private fun onRemoteEvent(e: RemoteClient.Event) {
        when (e) {
            is RemoteClient.Event.Ready -> {
                bridge.onProvider(e.provider.toString())
                bridge.onStatus("connected", "已连接")
                // 连上之前同步可能失败过（PC 刚起来、网络抖动）；
                // 这里补一次，否则模型就永远不加载了。
                if (!modelReady) {
                    val host = prefs.getString(KEY_HOST, null) ?: return
                    Log.i(TAG, "连接就绪但模型未就绪，重新同步")
                    syncModel(host, prefs.getInt(KEY_PORT, DEFAULT_PORT))
                }
            }
            is RemoteClient.Event.Transcript -> bridge.onTranscript(e.text)
            is RemoteClient.Event.Reply -> bridge.onReply(e.text, e.emotion)
            is RemoteClient.Event.Speech -> {
                // 播完后要告诉页面「说完了」，否则状态会一直停在「说话中…」。
                // AudioPlayer.play 的 onFinished 在播放线程里回调，必须 post 回主线程。
                val seconds = player.play(e.wavBase64) {
                    runOnUiThread { bridge.onSpeechEnd() }
                }
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
                lastStatus = state to e.detail
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

    override fun onTakePhoto(): String {
        val granted = ContextCompat.checkSelfPermission(this, Manifest.permission.CAMERA) ==
            PackageManager.PERMISSION_GRANTED
        if (!granted) {
            pendingPhotoAction = { onTakePhoto() }
            cameraPermLauncher.launch(Manifest.permission.CAMERA)
            return ""
        }
        photoTaker.take { b64, err ->
            if (b64 != null) {
                Log.i(TAG, "拍照完成 ${b64.length / 1024} KB")
                bridge.onPhoto(b64)
            } else {
                Log.w(TAG, "拍照失败: $err")
                bridge.onError(err ?: "拍照失败")
            }
        }
        return ""
    }

    override fun onPageAlive() {
        Log.i(TAG, "页面脚本就绪（modelReady=$modelReady）")
        pageReady = true
        // 连接可能早于页面加载完成，那样这条状态就丢了 —— 这里补发一次，
        // 否则界面会一直停在「连接你的 PC」上，看起来像没连上。
        lastStatus?.let { (state, detail) -> bridge.onStatus(state, detail) }
        if (modelReady) bridge.loadModel()
    }

    override fun onPageReady(info: String) {
        Log.i(TAG, "渲染成功: $info")
        pageReady = true
    }

    override fun onPageFailed(reason: String) {
        Log.e(TAG, "页面渲染失败: $reason")
        // 只有「显存不够」这类失败才值得降贴图精度。之前对任何失败都降档，
        // 结果 WebGL 被 Chromium 拉黑名单（模拟器上常见）时也降了档，
        // 白白把贴图从 4096 砍到 2048 却解决不了问题。
        val memoryRelated = Regex("context|contextlost|out of memory|OOM|texture|GPU",
            RegexOption.IGNORE_CASE).containsMatchIn(reason)
        if (memoryRelated && assets.textureScale > 0.6) {
            assets.textureScale = 0.5
            Log.w(TAG, "疑似显存不足，降低贴图精度到 ${assets.textureScale} 后重试")
            bridge.onStatus("retry", "显存不足，改用半尺寸贴图重试")
            webView.postDelayed({ webView.reload() }, 800)
        } else {
            bridge.onStatus("render_failed", reason)
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
        photoTaker.shutdown()
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
