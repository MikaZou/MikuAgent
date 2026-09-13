package com.mikuagent.pet.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTrack
import android.util.Base64
import android.util.Log
import kotlin.concurrent.thread
import kotlin.math.abs
import kotlin.math.max
import kotlin.math.sqrt

/**
 * 播放 PC 下发的语音，并同步产出**口型包络**。
 *
 * 口型为什么要在这里算：新模型的 `LipSync` 参数组是空的（cdi3 里
 * ParameterGroups 全为 0 个参数），所以不能用「参数组」机制，必须直接驱动
 * `ParamMouthOpenY`。而在原生侧按真实播放进度算 RMS 是最准的做法 ——
 * 页面里没有播放态的可靠时钟。
 *
 * 关键点：RMS 必须按 `playbackHeadPosition`（**已经播出去的**位置）来取，
 * 不能按「写进 AudioTrack 的位置」。AudioTrack 有缓冲，按写入进度算会让
 * 口型领先声音一大截。
 */
class AudioPlayer(private val onMouthLevel: (Float) -> Unit) {

    @Volatile
    private var track: AudioTrack? = null

    @Volatile
    private var playing = false

    private var pump: Thread? = null

    /** 解析出来的 PCM（16bit 小端单声道）与采样率 */
    private class Clip(val pcm: ShortArray, val sampleRate: Int)

    /** 播放 base64 WAV。返回时长（秒）；失败返回 0。 */
    fun play(wavBase64: String, onFinished: (() -> Unit)? = null): Double {
        stop()
        val clip = try {
            parse(base64ToBytes(wavBase64))
        } catch (e: Exception) {
            Log.e(TAG, "解析 WAV 失败", e)
            return 0.0
        }
        if (clip.pcm.isEmpty()) return 0.0

        val seconds = clip.pcm.size.toDouble() / clip.sampleRate
        val bytes = clip.pcm.size * 2

        val at = AudioTrack.Builder()
            .setAudioAttributes(
                AudioAttributes.Builder()
                    // 明确是「媒体」用途：不依赖浏览器那套自动播放策略
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build()
            )
            .setAudioFormat(
                AudioFormat.Builder()
                    .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
                    .setSampleRate(clip.sampleRate)
                    .setChannelMask(AudioFormat.CHANNEL_OUT_MONO)
                    .build()
            )
            .setBufferSizeInBytes(bytes)
            .setTransferMode(AudioTrack.MODE_STATIC)
            .build()

        try {
            at.write(clip.pcm, 0, clip.pcm.size)
            at.play()
        } catch (e: Exception) {
            Log.e(TAG, "播放失败", e)
            at.release()
            return 0.0
        }

        track = at
        playing = true
        Log.i(TAG, "播放 ${"%.2f".format(seconds)}s @ ${clip.sampleRate}Hz")

        pump = thread(name = "mouth-envelope") {
            val windowFrames = clip.sampleRate / ENVELOPE_HZ     // ~33ms
            var smoothed = 0f
            try {
                while (playing) {
                    val head = at.playbackHeadPosition
                    // 取「当前播放位置往前一点」的窗口，避免读到尚未播出的样本
                    val end = (head - windowFrames / 2).coerceAtLeast(0)
                    val start = (end - windowFrames).coerceAtLeast(0)
                    val level = if (end > start) rms(clip.pcm, start, end) else 0f

                    // 平滑：上升快、下降慢，观感更像说话而不是抽搐
                    smoothed = if (level > smoothed) {
                        smoothed * 0.4f + level * 0.6f
                    } else {
                        smoothed * 0.75f + level * 0.25f
                    }
                    onMouthLevel(smoothed.coerceIn(0f, 1f))

                    if (head >= clip.pcm.size) break
                    Thread.sleep(1000L / ENVELOPE_HZ)
                }
            } catch (_: InterruptedException) {
            } finally {
                onMouthLevel(0f)
                playing = false
                try {
                    at.stop()
                } catch (_: Exception) {
                }
                at.release()
                track = null
                Log.i(TAG, "播放结束")
                onFinished?.invoke()
            }
        }

        return seconds
    }

    fun stop() {
        playing = false
        pump?.interrupt()
        pump = null
        track?.let {
            try {
                if (it.playState == AudioTrack.PLAYSTATE_PLAYING) it.stop()
            } catch (_: Exception) {
            }
            it.release()
        }
        track = null
        onMouthLevel(0f)
    }

    val isPlaying: Boolean get() = playing

    // ------------------------------------------------------------------ 工具

    private fun rms(pcm: ShortArray, from: Int, to: Int): Float {
        var sum = 0.0
        var peak = 0
        var i = from
        while (i < to) {
            val v = abs(pcm[i].toInt())
            if (v > peak) peak = v
            sum += (pcm[i].toDouble() * pcm[i].toDouble())
            i++
        }
        val n = (to - from).coerceAtLeast(1)
        val rmsVal = sqrt(sum / n) / 32768.0
        // 纯 RMS 对轻辅音偏小，混一点峰值让口型更跟得上
        val peakVal = peak / 32768.0
        val mixed = rmsVal * 0.6 + peakVal * 0.4
        // 语音常见动态范围偏窄，做一次开方提升低音量的可辨识度
        return (sqrt(mixed) * LEVEL_GAIN).toFloat()
    }

    private fun base64ToBytes(s: String): ByteArray = Base64.decode(s, Base64.DEFAULT)

    /** 极简 WAV 解析：按块遍历找 fmt / data，兼容 PC 端 32kHz 单声道输出。 */
    private fun parse(wav: ByteArray): Clip {
        require(wav.size > 44) { "WAV 太短" }
        require(String(wav, 0, 4) == "RIFF" && String(wav, 8, 4) == "WAVE") { "不是 WAV" }

        var pos = 12
        var sampleRate = 32000
        var channels = 1
        var bits = 16
        var dataStart = -1
        var dataLen = 0

        while (pos + 8 <= wav.size) {
            val id = String(wav, pos, 4)
            val size = le32(wav, pos + 4)
            val body = pos + 8
            when (id) {
                "fmt " -> {
                    channels = le16(wav, body + 2)
                    sampleRate = le32(wav, body + 4)
                    bits = le16(wav, body + 14)
                }
                "data" -> {
                    dataStart = body
                    dataLen = minOf(size, wav.size - body)
                }
            }
            if (dataStart >= 0) break
            pos = body + size + (size and 1)   // 块按偶数字节对齐
        }
        require(dataStart >= 0) { "WAV 里没有 data 块" }
        require(bits == 16) { "只支持 16bit，实际 $bits" }

        val samples = dataLen / 2
        val out = ShortArray(samples)
        for (i in 0 until samples) {
            val lo = wav[dataStart + i * 2].toInt() and 0xFF
            val hi = wav[dataStart + i * 2 + 1].toInt()
            out[i] = ((hi shl 8) or lo).toShort()
        }
        if (channels > 1) {
            // 多声道就只取第一声道，PC 端目前恒为单声道，这里只是兜底
            val mono = ShortArray(samples / channels)
            for (i in mono.indices) mono[i] = out[i * channels]
            return Clip(mono, sampleRate)
        }
        return Clip(out, sampleRate)
    }

    private fun le32(b: ByteArray, o: Int) =
        (b[o].toInt() and 0xFF) or ((b[o + 1].toInt() and 0xFF) shl 8) or
            ((b[o + 2].toInt() and 0xFF) shl 16) or ((b[o + 3].toInt() and 0xFF) shl 24)

    private fun le16(b: ByteArray, o: Int) =
        (b[o].toInt() and 0xFF) or ((b[o + 1].toInt() and 0xFF) shl 8)

    companion object {
        private const val TAG = "AudioPlayer"

        /** 口型包络刷新率；30Hz 足够顺滑，又不至于太耗 */
        private const val ENVELOPE_HZ = 30
        private const val LEVEL_GAIN = 1.35
    }
}
