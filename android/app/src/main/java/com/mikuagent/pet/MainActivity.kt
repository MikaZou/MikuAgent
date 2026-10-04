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
import com.mikuagent.pet.brain.Brain
import com.mikuagent.pet.brain.BrainConfig
import com.mikuagent.pet.brain.BrainFactory
import com.mikuagent.pet.brain.BrainMode
import com.mikuagent.pet.brain.LocalBrain
import com.mikuagent.pet.brain.PcBrain
import com.mikuagent.pet.brain.Reply
import com.mikuagent.pet.brain.SecretStore
import com.mikuagent.pet.camera.PhotoTaker
import com.mikuagent.pet.memory.MemoryStore
import com.mikuagent.pet.model.ModelSync
import com.mikuagent.pet.net.RemoteClient
import com.mikuagent.pet.web.AssetServer
import com.mikuagent.pet.web.Bridge
import org.json.JSONObject
import android.util.Base64
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

    /**
     * 手机端自己的记忆库（SQLite）。
     *
     * 「对话在哪儿跑」不影响它：走 PC 时，PC 记它的库、手机记手机的库，
     * 连上之后靠同步合并（见 docs/TECHNICAL.md §5.12）。
     */
    private lateinit var memory: MemoryStore

    /** 最近一次用的会话 id；`resolveSession` 会按日期纠正，这里只是「同一天内的偏好」。 */
    private var sessionId: Long? = null

    /** 当前对话通道（api / pc）。onChat 每次都现挑，切换模式不用重启。 */
    private var brain: Brain? = null

    /** 手机端自己的语音合成 / 识别（只有独立模式用得到）。 */
    private lateinit var tts: com.mikuagent.pet.brain.Tts
    private val stt = com.mikuagent.pet.brain.Stt()

    private val scope = CoroutineScope(Dispatchers.Main + SupervisorJob())

    @Volatile
    private var pageReady = false

    @Volatile
    private var modelReady = false

    /** 最近一次连接状态。连接可能早于页面就绪，那时要在 onPageAlive 里补发。 */
    @Volatile
    private var lastStatus: Pair<String, String>? = null

    /**
     * 最近一次拿到的「可用模型清单」。
     *
     * 和 lastStatus 同一个道理：PC 的 `ready` 往往**早于**页面脚本就绪，
     * 那时 `evaluateJavascript` 打过去是空放一炮 —— 结果就是设置面板里
     * 列不出模型（真机实测踩到）。所以这里存一份补发。
     *
     * 不再需要单独存「当前用哪个」：手机自己就是权威，那个值永远是
     * [activeModelId]。
     */
    @Volatile
    private var lastModelsJson: String? = null

    /** 语音输入时是否自动附一张画面（对应 PC 的「视频对话」开关）。 */
    private var videoMode = false

    /**
     * 当前使用的 Live2D 模型 id。
     *
     * 初值取上次保存的；一旦连上 PC，就以 PC 下发的 `phone_model` 为准
     * （PC 控制台是唯一权威，见 ui/settings_dialog.py 的「手机端模型」）。
     */
    private var activeModelId = AssetServer.DEFAULT_MODEL

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
        // 先用上次用的模型起页面；连上 PC 后会以 PC 下发的为准
        activeModelId = prefs.getString(KEY_MODEL, null) ?: AssetServer.DEFAULT_MODEL
        assets.activeModelId = activeModelId
        memory = MemoryStore.get(this)
        tts = com.mikuagent.pet.brain.Tts(this)
        capture = AudioCapture()
        // 口型包络直接喂给页面；30Hz 的频率 evaluateJavascript 扛得住
        player = AudioPlayer { level -> if (pageReady) bridge.onMouth(level) }
        sync = ModelSync(this)
        sync.cleanupLegacyCache()
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
        Log.i(TAG, "模型目录 ${sync.modelDir(activeModelId).absolutePath}")
        webView.loadUrl(assets.indexUrl())

        // 恢复上次的 PC 地址；模拟器上直接给出宿主机地址，省掉手输
        val saved = prefs.getString(KEY_HOST, null)
        val host = saved ?: defaultHostForPlatform()
        if (host != null) {
            Log.i(TAG, "自动连接 PC 地址 $host（来源：${if (saved != null) "上次保存" else "模拟器默认"}）")
            startConnecting(host, prefs.getInt(KEY_PORT, DEFAULT_PORT))
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

    /**
     * 连接并同步模型。
     *
     * 端口以前是写死「读 SharedPreferences」的，现在由调用方给 —— 设置面板里
     * 可以改端口了（有些人 PC 上 8765 被占，只能换一个）。
     */
    private fun startConnecting(host: String, port: Int) {
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
     *
     * @param reloadAfter 换模型时要重载整个页面：模型换了，PIXI 里那个
     *   已经加载的实例没法「换皮」，重建页面是最干净的做法（而且页面自己
     *   会在 onPageAlive 里重新请求加载）。
     */
    private fun syncModel(host: String, port: Int, reloadAfter: Boolean = false) {
        if (!syncing.compareAndSet(false, true)) {
            Log.i(TAG, "已有同步在进行，跳过本次触发")
            return
        }
        scope.launch {
            try {
                val modelId = activeModelId
                val result = withContext(Dispatchers.IO) {
                    sync.sync(host, port, modelId) { p ->
                        bridge.onStatus("syncing", "模型 ${p.done}/${p.total}  ${p.percent}%")
                    }
                }
                when (result) {
                    is ModelSync.Result.Ready -> {
                        Log.i(TAG, "模型就绪：${result.modelId} 下载 ${result.downloaded} 个，跳过 ${result.skipped} 个")
                        assets.activeModelId = result.modelId
                        assets.profileJson = result.profileJson
                        modelReady = true
                        bridge.onStatus("model_ready", "模型已就绪")
                        if (reloadAfter) {
                            pageReady = false
                            webView.reload()
                        } else if (pageReady) {
                            bridge.loadModel()
                        }
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

    /**
     * 切到另一个模型：改本地状态 → 重新同步 → 重载页面。
     *
     * 模型文件按 id 分目录缓存，所以切回旧的模型几乎瞬间完成（清单比对上即命中）。
     */
    private fun switchModel(modelId: String, reason: String) {
        if (modelId.isBlank() || modelId == activeModelId) return
        Log.i(TAG, "切换模型 $activeModelId -> $modelId（$reason）")
        activeModelId = modelId
        assets.activeModelId = modelId
        prefs.edit().putString(KEY_MODEL, modelId).apply()
        modelReady = false
        val host = prefs.getString(KEY_HOST, null) ?: return
        syncModel(host, prefs.getInt(KEY_PORT, DEFAULT_PORT), reloadAfter = true)
    }

    private fun onRemoteEvent(e: RemoteClient.Event) {
        when (e) {
            is RemoteClient.Event.Ready -> {
                bridge.onProvider(e.provider.toString())
                lastModelsJson = e.modelsJson
                // 只取**可用清单**。`phone_model` 是 PC 那边记的一笔账，
                // **不用来覆盖本机选择** —— 否则用户在手机设置里选的模型，
                // 每次连上 PC 都会被重置回 PC 记着的那个。
                bridge.onModels(lastModelsJson ?: "[]", activeModelId)
                // 顺手告诉 PC 本机现在用哪个（PC 只记录，不会广播回来）
                remote.sendSetModel(activeModelId)
                bridge.onStatus("connected", "已连接")
                // 连上之前同步可能失败过（PC 刚起来、网络抖动）；
                // 这里补一次，否则模型就永远不加载了。
                if (!modelReady) {
                    val host = prefs.getString(KEY_HOST, null) ?: return
                    Log.i(TAG, "连接就绪但模型未就绪，重新同步")
                    syncModel(host, prefs.getInt(KEY_PORT, DEFAULT_PORT))
                }
            }
            is RemoteClient.Event.Config -> {
                // 旧版 PC 会广播「你该用哪个模型」。现在手机自己说了算，
                // 所以**不跟着改**；记一条方便排查新旧版本混用。
                Log.i(TAG, "PC 广播了模型 ${e.phoneModel}，本机保持 $activeModelId")
            }
            is RemoteClient.Event.Transcript -> bridge.onTranscript(e.text)
            // 走 PC 时回复是从 WS 回来的；统一走 deliverReply，
            // 这样「下面跑的是哪一端」对页面完全透明
            is RemoteClient.Event.Reply -> deliverReply(e.text, e.emotion)
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
        if (text.isBlank()) return
        if (!modelReady) Log.i(TAG, "模型还没就绪，但聊天不受影响")

        val current = pickBrain()
        brain = current
        Log.i(TAG, "对话走 ${current.kind}：${text.take(20)}（带图=${imageBase64 != null}）")

        // 页面给的是裸 base64（PC 那边直接 b64decode），这里还原成字节给两条路共用
        val image = imageBase64?.let {
            runCatching { Base64.decode(it, Base64.DEFAULT) }.getOrNull()
        }

        current.send(sessionId, text, image) { reply ->
            when (reply) {
                is Reply.Ok -> {
                    sessionId = reply.sessionId
                    deliverReply(reply.text, reply.emotion)
                }
                is Reply.Err -> runOnUiThread { bridge.onError(reply.message) }
            }
        }
    }

    /**
     * 按当前设置挑一条通道。**每次对话都现挑**，所以改完设置立刻生效，
     * 不需要重启 App。
     */
    private fun pickBrain(): Brain {
        val config = BrainConfig.load(this)
        val mode = BrainMode.load(prefs)
        val host = prefs.getString(KEY_HOST, null)
        return BrainFactory.pick(
            mode = mode,
            config = config,
            pcConfigured = !host.isNullOrBlank(),
            context = this,
            memory = memory,
            scope = scope,
            remote = remote,
        )
    }

    /**
     * 把一轮回复交给页面。PC 与手机两条路都汇到这里 ——
     * 页面、口型、动作逻辑因此完全不用关心后面是哪一端。
     */
    private fun deliverReply(text: String, emotion: String) {
        runOnUiThread { bridge.onReply(text, emotion) }
        // PC 会自己补一条 speech；手机端要自己合成（Phase 3）。
        if (brain?.deliversSpeech == false) {
            speakLocally(text, emotion)
        }
    }

    /** 手机端自己合成并播放（独立模式）。走 PC 时语音由 PC 下发，这里不会被调用。 */
    private fun speakLocally(text: String, emotion: String) {
        if (text.isBlank()) return
        scope.launch {
            val wav = withContext(Dispatchers.IO) {
                tts.synthesize(text, emotion, BrainConfig.load(this@MainActivity))
            }
            if (wav == null) {
                Log.d(TAG, "这一句没有语音：${text.take(12)}")
                bridge.onSpeechEnd()   // 让页面把「说话中…」收回去
                return@launch
            }
            val seconds = player.play(Base64.encodeToString(wav, Base64.NO_WRAP)) {
                runOnUiThread { bridge.onSpeechEnd() }
            }
            bridge.onSpeech(seconds)
        }
    }

    override fun onConnect(host: String, port: Int) {
        if (host.isBlank()) return
        Log.i(TAG, "用户指定 PC 地址: $host:$port")
        startConnecting(host, port)
    }

    /**
     * 设置面板要显示的「原生侧状态」。
     *
     * 页面拿不到 PC 地址/端口/贴图倍率（那些只在 SharedPreferences 与 AssetServer 里），
     * 所以在这里打包成 JSON 一次给它。
     */
    override fun onDeviceState(): String = JSONObject().apply {
        put("host", prefs.getString(KEY_HOST, "") ?: "")
        put("port", prefs.getInt(KEY_PORT, DEFAULT_PORT))
        put("model", activeModelId)
        put("connected", remote.isConnected)
        put("status", lastStatus?.second ?: "")
        put("textureScale", assets.textureScale)
        put("videoMode", videoMode)
        put("modelReady", modelReady)
        // 对话通道：页面要显示「现在到底谁在回答」
        // 注意 `this` —— 这里在 JSONObject().apply{} 里，不加限定符会拿到 JSONObject
        put("brainMode", BrainMode.load(prefs).id)
        put("brainKind", pickBrain().kind)
        put("hasDeepseekKey", BrainConfig.load(this@MainActivity).hasDeepseekKey)
        put("hasMinimaxKey", BrainConfig.load(this@MainActivity).hasMinimaxKey)
        put("secretsEncrypted", SecretStore.isEncrypted(this@MainActivity))
        put("memory", JSONObject().apply {
            memory.counts().forEach { (k, v) -> put(k, v) }
        })
        val cfg = BrainConfig.load(this@MainActivity)
        put("tts", JSONObject().apply {
            put("status", tts.status(cfg))
            put("cacheBytes", tts.cacheSizeBytes())
            tts.last?.let { s ->
                put("last", JSONObject().apply {
                    put("textChars", s.textChars)
                    put("cleanChars", s.cleanChars)
                    put("sentChars", s.sentChars)
                    put("format", s.format)
                    put("bytes", s.bytes)
                    put("billedChars", s.billedChars)
                    put("audioMs", s.audioMs)
                    put("fromCache", s.fromCache)
                })
            }
        })
    }.toString()

    /**
     * 保存 API 配置。入参 JSON 的字段名与 PC 的 `.env` 一致，
     * 所以 PC 设置窗口「复制 API 配置」出来的那段能原样粘进来。
     */
    override fun onSaveApiConfig(json: String): String = try {
        val used = BrainConfig.importJson(this, json)
        Log.i(TAG, "API 配置已保存：$used")
        // 配置变了，下一次对话要按新配置重挑通道
        brain = null
        used.joinToString(",")
    } catch (e: Exception) {
        Log.w(TAG, "API 配置保存失败", e)
        "ERR:${e.message}"
    }

    /** Key 只回脱敏形式 —— 页面没必要（也不该）拿到明文。 */
    override fun onApiConfig(): String {
        val c = BrainConfig.load(this)
        return JSONObject().apply {
            put("deepseekKey", c.masked(c.deepseekKey))
            put("minimaxKey", c.masked(c.minimaxKey))
            put("deepseekBaseUrl", c.deepseekBaseUrl)
            put("deepseekModel", c.deepseekModel)
            put("temperature", c.temperature)
            put("thinking", c.thinking)
            put("minimaxBaseUrl", c.minimaxBaseUrl)
            put("minimaxTtsModel", c.minimaxTtsModel)
            put("minimaxVoiceId", c.minimaxVoiceId)
            put("minimaxSpeed", c.minimaxSpeed)
            put("minimaxAsrModel", c.minimaxAsrModel)
            put("sttLanguage", c.sttLanguage)
            put("visionDetail", c.visionDetail)
            put("maxHistory", c.maxHistory)
            put("hasDeepseekKey", c.hasDeepseekKey)
            put("hasMinimaxKey", c.hasMinimaxKey)
            put("secretsEncrypted", SecretStore.isEncrypted(this@MainActivity))
        }.toString()
    }

    override fun onSetBrainMode(mode: String) {
        val m = BrainMode.from(mode)
        BrainMode.save(prefs, m)
        brain = null   // 下次对话按新模式重挑
        Log.i(TAG, "对话通道切换为 ${m.id}（下次对话生效）")
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
        Log.i(TAG, "收下语音 ${"%.2f".format(seconds)}s")

        val current = pickBrain()
        brain = current

        // 和 PC 端「视频对话」同一套做法：**不是**持续推流，而是在说话结束时
        // 自动配一张画面给 Miku 看。手机上没有摄像头预览，所以只在开头拍一帧；
        // 这样既能看到主人，又不会一直开着摄像头（指示灯常亮是隐私问题）
        // 也不费流量。
        // PhotoTaker 给的是 base64 字符串（与 PC 的 sendAudio 同一个形状）
        val withPhoto: (String?) -> Unit = { photo ->
            if (current is LocalBrain) {
                transcribeThenChat(b64, photo)
            } else {
                if (photo != null) {
                    Log.i(TAG, "语音附带画面 ${photo.length / 1024}KB")
                    remote.sendAudio(b64, photo)
                } else {
                    remote.sendAudio(b64)
                }
            }
        }

        if (videoMode) {
            photoTaker.take { photo, err ->
                if (photo == null) Log.w(TAG, "附带画面失败，只发语音：$err")
                withPhoto(photo)
            }
        } else {
            withPhoto(null)
        }
    }

    /**
     * 独立模式下：**手机自己**把录音转成文字，然后当成一次普通对话。
     *
     * 与 PC 路径的差别只有「谁来转写」—— 转完之后走的是同一套 chat，
     * 页面看到的 `transcript` / `reply` 事件也一模一样。
     */
    private fun transcribeThenChat(wavBase64: String, photoBase64: String?) {
        scope.launch {
            val wav = runCatching { Base64.decode(wavBase64, Base64.DEFAULT) }.getOrNull()
            if (wav == null) {
                bridge.onError("录音解码失败")
                return@launch
            }
            val text = withContext(Dispatchers.IO) {
                stt.transcribe(wav, BrainConfig.load(this@MainActivity))
            }
            if (text == null) {
                bridge.onError(stt.lastError.ifBlank { "识别失败" })
                return@launch
            }
            if (text.isBlank()) {
                // 转写为空：与 PC 一样回一条空 transcript，页面会提示「没听清」
                bridge.onTranscript("")
                return@launch
            }
            bridge.onTranscript(text)
            val local = brain as? LocalBrain ?: return@launch
            // 页面/相机的图片是 base64，LocalBrain 要字节
            val image = photoBase64?.let {
                runCatching { Base64.decode(it, Base64.DEFAULT) }.getOrNull()
            }
            local.send(sessionId, text, image) { reply ->
                when (reply) {
                    is Reply.Ok -> {
                        sessionId = reply.sessionId
                        deliverReply(reply.text, reply.emotion)
                    }
                    is Reply.Err -> runOnUiThread { bridge.onError(reply.message) }
                }
            }
        }
    }

    override fun onCancelRecording() = capture.cancel()

    /** 手机端的「视频对话」开关：只影响说话时是否附带一帧，不做持续推流。 */
    override fun onSetVideoMode(on: Boolean) {
        videoMode = on
        Log.i(TAG, "视频对话（说话时附一帧）${if (on) "开启" else "关闭"}")
    }

    /**
     * 手机界面上的模型单选项。
     *
     * **本地直接切**：手机就是「自己用哪个模型」的主人（PC 上已经没有这个设置项了）。
     * 切完顺手把选择告诉 PC 记一笔 —— 只影响 `data/model_prefs.json` 的 `phone`
     * 与老写法的 `/model/manifest`，PC 不会再广播回来。
     */
    override fun onSetModel(id: String) {
        if (id.isBlank() || id == activeModelId) return
        Log.i(TAG, "手机设置里换模型 -> $id")
        switchModel(id, "手机设置")
        remote.sendSetModel(id)
    }

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
        // 模型清单同理：ready 早于页面就绪时那次 JS 调用是空放的
        val models = lastModelsJson
        if (models != null) {
            bridge.onModels(models, activeModelId)
        }
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
        private const val KEY_MODEL = "model_id"

        /**
         * 模拟器访问宿主机的固定地址；真机需要填 PC 的局域网 IP
         * （PC 端气泡里会显示，形如 192.168.20.102）。
         */
        const val DEFAULT_PORT = 8765
        const val EMULATOR_HOST = "10.0.2.2"
    }
}
