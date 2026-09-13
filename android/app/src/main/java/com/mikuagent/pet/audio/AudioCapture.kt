package com.mikuagent.pet.audio

import android.annotation.SuppressLint
import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import android.util.Base64
import android.util.Log
import java.io.ByteArrayOutputStream

/**
 * 按住说话：麦克风采集 → 16kHz 单声道 WAV → base64。
 *
 * 采样率必须与 PC 端 `STT_SAMPLE_RATE`（默认 16000）一致。
 * 之前踩过这个坑：把 32k 采样的数据当成 16k 写进 WAV 头，ASR 会把
 * 「初音未来」听成别的词 —— 采样率写错不会报错，只会静默识别错。
 *
 * 这是原生实现，不受浏览器「安全上下文」限制 —— 浏览器版 phone.html
 * 在 `http://192.168.x.x` 下 `navigator.mediaDevices` 是 undefined，
 * 麦克风完全用不了。
 */
class AudioCapture {

    @Volatile
    private var recorder: AudioRecord? = null

    @Volatile
    private var running = false

    private var thread: Thread? = null
    private val pcm = ByteArrayOutputStream()

    val isRecording: Boolean get() = running

    /** 开始录音。返回 null 表示成功，否则是给用户看的错误信息。 */
    @SuppressLint("MissingPermission")
    fun start(): String? {
        if (running) return "已经在录音了"

        val minBuf = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL, ENCODING)
        if (minBuf <= 0) return "设备不支持 ${SAMPLE_RATE}Hz 单声道录音"

        val rec = try {
            AudioRecord(
                MediaRecorder.AudioSource.VOICE_RECOGNITION,
                SAMPLE_RATE, CHANNEL, ENCODING,
                minBuf * 2,
            )
        } catch (e: Exception) {
            return "创建录音器失败：${e.message}"
        }

        if (rec.state != AudioRecord.STATE_INITIALIZED) {
            rec.release()
            return "录音器初始化失败（权限被拒或麦克风被占用？）"
        }

        pcm.reset()
        recorder = rec
        running = true

        thread = Thread {
            val buf = ByteArray(minBuf)
            try {
                rec.startRecording()
                while (running) {
                    val n = rec.read(buf, 0, buf.size)
                    if (n > 0) {
                        synchronized(pcm) { pcm.write(buf, 0, n) }
                    } else if (n < 0) {
                        Log.w(TAG, "read 返回 $n")
                        break
                    }
                }
            } catch (e: Exception) {
                Log.e(TAG, "录音线程异常", e)
            } finally {
                try {
                    if (rec.recordingState == AudioRecord.RECORDSTATE_RECORDING) rec.stop()
                } catch (_: Exception) {
                }
                rec.release()
                recorder = null
                Log.i(TAG, "录音线程退出")
            }
        }.also { it.start() }

        Log.i(TAG, "开始录音 ${SAMPLE_RATE}Hz 单声道")
        return null
    }

    /** 停止并返回 (base64 WAV, 时长秒)。没有有效音频时返回 null。 */
    fun stop(): Pair<String, Double>? {
        if (!running) return null
        running = false
        thread?.join(1500)
        thread = null

        val data = synchronized(pcm) { pcm.toByteArray() }
        val seconds = data.size.toDouble() / (SAMPLE_RATE * 2)
        if (seconds < MIN_SECONDS) {
            Log.i(TAG, "录音太短（${"%.2f".format(seconds)}s），丢弃")
            return null
        }
        Log.i(TAG, "录音结束 ${"%.2f".format(seconds)}s / ${data.size / 1024}KB")
        return Base64.encodeToString(wrapWav(data), Base64.NO_WRAP) to seconds
    }

    /** 放弃本次录音。 */
    fun cancel() {
        running = false
        thread?.join(800)
        thread = null
        synchronized(pcm) { pcm.reset() }
    }

    /** 给裸 PCM 套一个 WAV 头（单声道 16bit）。 */
    private fun wrapWav(pcmData: ByteArray): ByteArray {
        val out = ByteArrayOutputStream(pcmData.size + 44)
        val total = pcmData.size + 36
        val byteRate = SAMPLE_RATE * 2

        fun le32(v: Int) = byteArrayOf(
            (v and 0xFF).toByte(), ((v shr 8) and 0xFF).toByte(),
            ((v shr 16) and 0xFF).toByte(), ((v shr 24) and 0xFF).toByte(),
        )
        fun le16(v: Int) = byteArrayOf((v and 0xFF).toByte(), ((v shr 8) and 0xFF).toByte())

        out.write("RIFF".toByteArray())
        out.write(le32(total))
        out.write("WAVE".toByteArray())
        out.write("fmt ".toByteArray())
        out.write(le32(16))
        out.write(le16(1))                 // PCM
        out.write(le16(1))                 // 单声道
        out.write(le32(SAMPLE_RATE))
        out.write(le32(byteRate))
        out.write(le16(2))                 // block align
        out.write(le16(16))                // bits per sample
        out.write("data".toByteArray())
        out.write(le32(pcmData.size))
        out.write(pcmData)
        return out.toByteArray()
    }

    companion object {
        private const val TAG = "AudioCapture"

        /** 必须与 PC 端 config.STT_SAMPLE_RATE 一致 */
        const val SAMPLE_RATE = 16000
        private const val CHANNEL = AudioFormat.CHANNEL_IN_MONO
        private const val ENCODING = AudioFormat.ENCODING_PCM_16BIT

        /** 太短多半是误触 */
        private const val MIN_SECONDS = 0.35
    }
}
