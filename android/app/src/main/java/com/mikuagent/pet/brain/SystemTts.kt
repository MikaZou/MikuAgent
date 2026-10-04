package com.mikuagent.pet.brain

import android.content.Context
import android.speech.tts.TextToSpeech
import android.speech.tts.UtteranceProgressListener
import android.util.Log
import java.io.File
import java.util.Locale
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 系统 TTS 兜底：没配 MiniMax Key 时**至少要有声音**。
 *
 * 代价说清楚：音色是系统音色，**不是初音**；中文效果取决于手机装的 TTS 引擎
 * （国行 ROM 上通常有，但质量参差）。设置面板里会写明当前用的是哪一种，
 * 不让用户以为「克隆音色怎么变味了」。
 *
 * 用 `synthesizeToFile` 而不是 `speak()`：前者给的是 WAV 文件，
 * 能直接喂 [com.mikuagent.pet.audio.AudioPlayer]，于是**口型包络照样有**
 * （`speak()` 只在系统那边播，拿不到 PCM）。
 */
class SystemTts(private val context: Context) {

    @Volatile
    private var engine: TextToSpeech? = null

    @Volatile
    private var ready = false

    /** 有可用的系统 TTS 引擎（初始化过一次之后才准）。 */
    val available: Boolean get() = ready

    /**
     * 合成到 WAV 字节。失败返回 null。
     *
     * ⚠️ **阻塞**：`TextToSpeech` 的初始化和合成都是异步回调的，这里用
     * `CountDownLatch` 等。所以**不能在主线程调用**（调用方都是 IO 线程）。
     */
    fun synthesize(text: String): ByteArray? {
        val tts = ensure() ?: return null
        val out = File.createTempFile("sys-tts-", ".wav", context.cacheDir)
        val latch = CountDownLatch(1)
        // 局部变量不能用 @Volatile（那是属性的注解），用 AtomicBoolean
        val ok = java.util.concurrent.atomic.AtomicBoolean(false)

        tts.setOnUtteranceProgressListener(object : UtteranceProgressListener() {
            override fun onStart(utteranceId: String?) = Unit

            override fun onDone(utteranceId: String?) {
                ok.set(true)
                latch.countDown()
            }

            @Deprecated("被带 errorCode 的重载取代，但基类要求实现")
            override fun onError(utteranceId: String?) {
                latch.countDown()
            }

            override fun onError(utteranceId: String?, errorCode: Int) {
                Log.w(TAG, "系统 TTS 失败 errorCode=$errorCode")
                latch.countDown()
            }
        })

        val utteranceId = "miku-${System.nanoTime()}"
        val queued = tts.synthesizeToFile(text, null, out, utteranceId)
        if (queued != TextToSpeech.SUCCESS) {
            out.delete()
            return null
        }
        if (!latch.await(SYNTH_TIMEOUT_SECONDS, TimeUnit.SECONDS) || !ok.get()) {
            Log.w(TAG, "系统 TTS 超时或失败")
            out.delete()
            return null
        }
        return try {
            out.readBytes().also { out.delete() }
        } catch (e: Exception) {
            Log.w(TAG, "读系统 TTS 产物失败：${e.message}")
            out.delete()
            null
        }
    }

    private fun ensure(): TextToSpeech? {
        engine?.let { return if (ready) it else null }
        synchronized(this) {
            engine?.let { return if (ready) it else null }
            val latch = CountDownLatch(1)
            val created = TextToSpeech(context) { status ->
                ready = status == TextToSpeech.SUCCESS
                if (!ready) Log.w(TAG, "系统 TTS 初始化失败 status=$status")
                latch.countDown()
            }
            engine = created
            latch.await(INIT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
            if (ready) {
                // 中文优先；语言不支持时系统会回退，不强行失败
                val zh = created.setLanguage(Locale.SIMPLIFIED_CHINESE)
                if (zh == TextToSpeech.LANG_MISSING_DATA || zh == TextToSpeech.LANG_NOT_SUPPORTED) {
                    Log.w(TAG, "系统 TTS 不支持中文，仍按默认语言尝试")
                }
                created.setSpeechRate(1.05f)
                created.setPitch(1.25f)   // 稍微高一点，更接近少女音
                Log.i(TAG, "系统 TTS 就绪：${created.defaultEngine}")
            }
            return if (ready) created else null
        }
    }

    fun shutdown() {
        runCatching { engine?.stop() }
        runCatching { engine?.shutdown() }
        engine = null
        ready = false
    }

    companion object {
        private const val TAG = "SystemTts"
        private const val INIT_TIMEOUT_SECONDS = 6L
        private const val SYNTH_TIMEOUT_SECONDS = 20L
    }
}
