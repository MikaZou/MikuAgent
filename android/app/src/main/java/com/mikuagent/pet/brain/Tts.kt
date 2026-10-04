package com.mikuagent.pet.brain

import android.content.Context
import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.io.File
import java.util.concurrent.TimeUnit

/**
 * 手机端的语音合成：直连 MiniMax（和 PC 用的是同一个克隆音色）。
 *
 * 相对本地 GPT-SoVITS 的差别（PC 那边的取舍同样适用）：省掉 1.4~2.2GB 显存与
 * 一个常驻进程，代价是联网 + 按量计费。
 *
 * **产出统一是 WAV**：[com.mikuagent.pet.audio.AudioPlayer] 吃 WAV，
 * 它自己解析 RIFF 并按真实播放进度算口型包络 —— 所以只要拿到 WAV 字节，
 * 播放与口型那两段代码一行都不用改。
 */
class Tts(private val context: Context) {

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(30, TimeUnit.SECONDS)
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** 系统 TTS 兜底（没配 MiniMax Key 时至少有声音）。 */
    private val system = SystemTts(context)

    private val cacheDir: File by lazy {
        File(context.filesDir, "tts-cache").apply { mkdirs() }
    }

    /** 当前这次合成的字数，用于填 [last]。 */
    private var pending: Triple<Int, Int, Int>? = null

    @Volatile
    private var lastFormat: String = ""

    @Volatile
    private var lastBilled: Int = 0

    @Volatile
    private var lastAudioMs: Int = 0

    /** 最近一次合成的实况。给设置面板和排查用 ——
     *  MIUI 的 logcat 经常把整个应用的日志吞掉，不能只靠日志确认「到底合成了什么」。 */
    data class LastSynth(
        val textChars: Int,
        val cleanChars: Int,
        val sentChars: Int,
        val format: String,
        val bytes: Int,
        val billedChars: Int,
        val audioMs: Int,
        val fromCache: Boolean,
    )

    @Volatile
    var last: LastSynth? = null
        private set

    /**
     * 合成一句。返回 WAV 字节；不可用/无内容时返回 null。
     *
     * 与 PC 一样有**磁盘缓存**：Miku 有大量重复问候语，命中直接复用，
     * 既快又不重复计费。
     */
    fun synthesize(text: String, emotion: String, config: BrainConfig): ByteArray? {
        val clean = TtsText.normalize(text)
        if (clean.isEmpty()) return null
        val body = clean.take(MAX_CHARS)

        if (!config.hasMinimaxKey) {
            // 没配 Key：退到系统 TTS。音色不是初音，但「至少有声音」比静默好。
            val wav = system.synthesize(body)
            last = LastSynth(text.length, clean.length, body.length, "system",
                wav?.size ?: 0, 0, 0, false)
            return wav
        }

        val mmEmotion = TtsText.MINIMAX_EMOTION[emotion.uppercase()] ?: "neutral"
        val speed = TtsText.minimaxSpeed(config.minimaxSpeed, mmEmotion, text, emotion)
        val voice = config.minimaxVoiceId.ifBlank { TtsText.MINIMAX_FALLBACK_VOICE }

        val key = Codec.sha1Hex(
            listOf(
                "minimax", config.minimaxBaseUrl, config.minimaxTtsModel, voice,
                speed.toString(), mmEmotion, body,
            ).joinToString("|")
        )
        val cached = File(cacheDir, "$key.wav")
        if (cached.exists() && cached.length() > 1024) {
            cached.setLastModified(System.currentTimeMillis())
            val bytes = cached.readBytes()
            last = LastSynth(text.length, clean.length, body.length, "cache",
                bytes.size, 0, 0, true)
            return bytes
        }
        this.pending = Triple(text.length, clean.length, body.length)

        val wav = try {
            // 先要 wav；接口如果不认这个格式，退到 pcm 再自己补 WAV 头
            // （两条路都不需要音频解码器）
            requestWav(config, body, voice, mmEmotion, speed)
        } catch (e: Exception) {
            Log.w(TAG, "MiniMax 合成失败：${e.message}")
            return null
        }

        runCatching { cached.writeBytes(wav) }
        evictCache()
        val p = pending
        last = LastSynth(
            textChars = p?.first ?: 0,
            cleanChars = p?.second ?: 0,
            sentChars = p?.third ?: 0,
            format = lastFormat,
            bytes = wav.size,
            billedChars = lastBilled,
            audioMs = lastAudioMs,
            fromCache = false,
        )
        return wav
    }

    private fun requestWav(
        config: BrainConfig,
        text: String,
        voice: String,
        mmEmotion: String,
        speed: Double,
    ): ByteArray {
        var lastError: Exception? = null
        for (format in listOf("wav", "pcm")) {
            try {
                val audio = post(config, text, voice, mmEmotion, speed, format)
                return if (format == "wav") {
                    audio
                } else {
                    // 裸 PCM（16bit 小端单声道）→ 本地补 44 字节 WAV 头
                    Codec.pcm16ToWav(audio, SAMPLE_RATE, channels = 1)
                }
            } catch (e: Exception) {
                lastError = e
                Log.w(TAG, "format=$format 不行（${e.message}），换下一个")
            }
        }
        throw lastError ?: RuntimeException("合成失败")
    }

    private fun post(
        config: BrainConfig,
        text: String,
        voice: String,
        mmEmotion: String,
        speed: Double,
        format: String,
    ): ByteArray {
        val payload = JSONObject().apply {
            put("model", config.minimaxTtsModel)
            put("text", text)
            put("stream", false)
            put(
                "voice_setting",
                JSONObject().apply {
                    put("voice_id", voice)
                    // 克隆自 v4c 发布会致辞，那段本身语速偏慢；基准 1.8 才接近自然语速，
                    // 再按情绪与句式补偿（见 TtsText.minimaxSpeed）
                    put("speed", speed)
                    put("vol", 1.0)
                    put("pitch", 0)
                    put("emotion", mmEmotion)
                },
            )
            put(
                "audio_setting",
                JSONObject().apply {
                    put("sample_rate", SAMPLE_RATE)
                    put("bitrate", 128000)
                    put("format", format)
                    put("channel", 1)
                },
            )
        }

        val data = postWithRetry(config, payload)
        val hex = data.optJSONObject("data")?.optString("audio").orEmpty()
        if (hex.isEmpty()) throw RuntimeException("MiniMax 没有返回音频数据")
        val info = data.optJSONObject("extra_info")
        lastFormat = format
        lastBilled = info?.optString("usage_characters")?.toIntOrNull() ?: 0
        lastAudioMs = info?.optString("audio_length")?.toIntOrNull() ?: 0
        Log.i(
            TAG,
            "MiniMax 合成 ${info?.optString("audio_length", "?")}ms / " +
                "计费 ${info?.optString("usage_characters", "?")} 字 / format=$format",
        )
        return Codec.hexToBytes(hex)
    }

    /**
     * 对限流与瞬时故障做指数退避重试。
     *
     * 限流**不能**当硬失败：MiniMax 并发稍高时返回 HTTP 200 但
     * `base_resp.status_code=1002`（rate limit），当成失败的话这一句就没声音。
     * 鉴权/余额/音色这类硬错误重试也没用，直接失败并给出可读提示。
     */
    private fun postWithRetry(config: BrainConfig, payload: JSONObject): JSONObject {
        val retryable = setOf(1002, 1039, 1042)   // 限流 / 服务繁忙
        var last = "未知错误"
        for (attempt in 0 until RETRIES) {
            last = try {
                val request = Request.Builder()
                    .url("${config.minimaxBaseUrl}/v1/t2a_v2")
                    .header("Authorization", "Bearer ${config.minimaxKey}")
                    .header("Content-Type", "application/json")
                    .post(payload.toString().toRequestBody(JSON_MEDIA))
                    .build()
                http.newCall(request).execute().use { resp ->
                    val text = resp.body?.string().orEmpty()
                    val json = runCatching { JSONObject(text) }.getOrNull()
                    if (json == null) {
                        "返回非 JSON（HTTP ${resp.code}）：${text.take(120)}"
                    } else {
                        val base = json.optJSONObject("base_resp") ?: JSONObject()
                        val code = if (base.has("status_code")) base.optInt("status_code") else null
                        if (resp.isSuccessful && (code == null || code == 0)) {
                            return json
                        }
                        val hint = when (code) {
                            1004 -> "（鉴权失败，检查 MiniMax API Key）"
                            1008 -> "（余额不足）"
                            2056 -> "（音色不存在或已过期，克隆音色 7 天未使用会被删除，需重新克隆）"
                            else -> ""
                        }
                        val msg = "HTTP ${resp.code} code=$code ${base.optString("status_msg")}$hint"
                        // 硬错误不重试
                        if (code !in retryable && (code != null || resp.code < 500)) {
                            throw RuntimeException("MiniMax 合成失败 $msg")
                        }
                        msg
                    }
                }
            } catch (e: RuntimeException) {
                throw e
            } catch (e: Exception) {
                "请求异常：${e.message}"
            }
            if (attempt < RETRIES - 1) {
                val delay = minOf(8.0, 1.5 * (1 shl attempt)).toLong() * 1000
                Log.w(TAG, "MiniMax 重试 ${attempt + 2}/$RETRIES（$last），${delay}ms 后")
                Thread.sleep(delay)
            }
        }
        throw RuntimeException("MiniMax 合成失败（已重试 $RETRIES 次）$last")
    }

    /** LRU 淘汰：按最近使用时间把缓存压到上限以内（缓存只增不减会一直涨）。 */
    private fun evictCache() {
        val files = cacheDir.listFiles() ?: return
        var total = files.sumOf { it.length() }
        if (total <= MAX_CACHE_BYTES) return
        for (f in files.sortedBy { it.lastModified() }) {
            if (total <= MAX_CACHE_BYTES) break
            val size = f.length()
            if (f.delete()) total -= size
        }
    }

    /** 设置面板显示用。 */
    fun status(config: BrainConfig): String = when {
        config.hasMinimaxKey -> "MiniMax（${config.minimaxVoiceId.ifBlank { "默认少女音" }}）"
        system.available -> "系统 TTS（未配 MiniMax Key，音色不是初音）"
        else -> "不可用"
    }

    fun shutdown() {
        system.shutdown()
    }

    /** 缓存占用（设置面板/排查用）。 */
    fun cacheSizeBytes(): Long = (cacheDir.listFiles() ?: emptyArray()).sumOf { it.length() }

    companion object {
        private const val TAG = "Tts"
        private const val SAMPLE_RATE = 32000
        private const val MAX_CHARS = 200
        private const val RETRIES = 3
        private const val MAX_CACHE_BYTES = 200L * 1024 * 1024
        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()
    }
}
