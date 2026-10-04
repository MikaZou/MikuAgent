package com.mikuagent.pet.brain

import java.security.MessageDigest

/** 小工具：十六进制、摘要、PCM→WAV 头。都没什么依赖，放一起。 */
object Codec {

    /** `data.audio` 是 hex 编码的（MiniMax 的约定）。 */
    fun hexToBytes(hex: String): ByteArray {
        val clean = hex.trim()
        require(clean.length % 2 == 0) { "hex 长度不是偶数：${clean.length}" }
        val out = ByteArray(clean.length / 2)
        for (i in out.indices) {
            val hi = Character.digit(clean[i * 2], 16)
            val lo = Character.digit(clean[i * 2 + 1], 16)
            require(hi >= 0 && lo >= 0) { "不是合法 hex：${clean.substring(i * 2, i * 2 + 2)}" }
            out[i] = ((hi shl 4) or lo).toByte()
        }
        return out
    }

    fun sha1Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-1").digest(bytes)
            .joinToString("") { "%02x".format(it) }

    fun sha1Hex(text: String): String = sha1Hex(text.toByteArray(Charsets.UTF_8))

    /**
     * 给裸 PCM 套一个 44 字节的 WAV 头（16bit 小端、单声道/多声道）。
     *
     * 为什么需要它：MiniMax 可以按 `format=pcm` 返回裸 PCM（这个格式最稳），
     * 而手机上的播放器 [com.mikuagent.pet.audio.AudioPlayer] 吃的是 WAV ——
     * 它自己解析 RIFF 头、按真实播放进度算口型包络。在本地补个头就能直接复用，
     * **不需要引入任何音频解码器**（mp3 就得拉 MediaCodec 进来）。
     */
    fun pcm16ToWav(pcm: ByteArray, sampleRate: Int, channels: Int = 1): ByteArray {
        val byteRate = sampleRate * channels * 2
        val out = ByteArray(44 + pcm.size)
        fun putInt(offset: Int, value: Int) {
            out[offset] = (value and 0xFF).toByte()
            out[offset + 1] = ((value shr 8) and 0xFF).toByte()
            out[offset + 2] = ((value shr 16) and 0xFF).toByte()
            out[offset + 3] = ((value shr 24) and 0xFF).toByte()
        }
        fun putShort(offset: Int, value: Int) {
            out[offset] = (value and 0xFF).toByte()
            out[offset + 1] = ((value shr 8) and 0xFF).toByte()
        }
        "RIFF".toByteArray(Charsets.US_ASCII).copyInto(out, 0)
        putInt(4, 36 + pcm.size)
        "WAVE".toByteArray(Charsets.US_ASCII).copyInto(out, 8)
        "fmt ".toByteArray(Charsets.US_ASCII).copyInto(out, 12)
        putInt(16, 16)              // fmt 块长度
        putShort(20, 1)             // PCM
        putShort(22, channels)
        putInt(24, sampleRate)
        putInt(28, byteRate)
        putShort(32, channels * 2)  // block align
        putShort(34, 16)            // bits per sample
        "data".toByteArray(Charsets.US_ASCII).copyInto(out, 36)
        putInt(40, pcm.size)
        pcm.copyInto(out, 44)
        return out
    }

    /** WAV 头里的采样率（给「裸 PCM 补头」时用）。 */
    fun wavSampleRate(wav: ByteArray): Int {
        if (wav.size < 28) return 0
        return (wav[24].toInt() and 0xFF) or
            ((wav[25].toInt() and 0xFF) shl 8) or
            ((wav[26].toInt() and 0xFF) shl 16) or
            ((wav[27].toInt() and 0xFF) shl 24)
    }
}
