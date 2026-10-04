package com.mikuagent.pet.brain

import android.util.Log
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 手机端语音识别：直连 MiniMax ASR。对应 PC 的 `SpeechToText._transcribe_minimax`。
 *
 * 为什么不做本地 Whisper：手机上跑 small 模型要几百 MB 内存、模型文件也不能随包
 * 分发（体积 + 授权），而云端 ASR 的延迟本来就在可接受范围内。
 * PC 那边保留本地 Whisper 作为**离线兜底**，手机没有这个选项 —— 没网就没法识别，
 * 会明确告诉用户，而不是「按了说话却什么都没发生」。
 */
class Stt {

    private val http: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(10, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)   // 语音有几百 KB
        .readTimeout(60, TimeUnit.SECONDS)
        .build()

    /** 识别结果。`null` 表示失败（原因见 [lastError]），空串表示「没听清」。 */
    fun transcribe(wav: ByteArray, config: BrainConfig): String? {
        if (!config.hasMinimaxKey) {
            lastError = "没配 MiniMax API Key，手机端语音输入不可用（设置 → API 配置）"
            return null
        }
        val request = Request.Builder()
            .url("${config.minimaxBaseUrl}/v1/speech_to_text")
            .header("Authorization", "Bearer ${config.minimaxKey}")
            // BCP-47，如 zh / ja
            .apply { if (config.sttLanguage.isNotBlank()) header("language", config.sttLanguage) }
            .post(
                MultipartBody.Builder().setType(MultipartBody.FORM)
                    .addFormDataPart("model", config.minimaxAsrModel)
                    .addFormDataPart("response_format", "json")
                    .addFormDataPart(
                        "file", "speech.wav",
                        wav.toRequestBody("audio/wav".toMediaType()),
                    )
                    .build()
            )
            .build()

        return try {
            http.newCall(request).execute().use { resp ->
                val body = resp.body?.string().orEmpty()
                val json = runCatching { JSONObject(body) }.getOrNull()
                if (json == null) {
                    lastError = "ASR 返回非 JSON（HTTP ${resp.code}）：${body.take(120)}"
                    return null
                }
                val base = json.optJSONObject("base_resp") ?: JSONObject()
                val code = if (base.has("status_code")) base.optInt("status_code") else null
                if (!resp.isSuccessful || (code != null && code != 0)) {
                    val hint = when (code) {
                        1004 -> "（鉴权失败，检查 MiniMax API Key）"
                        1008 -> "（余额不足）"
                        else -> ""
                    }
                    lastError = "ASR 失败 HTTP ${resp.code} code=$code " +
                        "${base.optString("status_msg")}$hint"
                    return null
                }
                val text = json.optString("text").trim()
                Log.i(TAG, "MiniMax 转写 ${json.optString("duration", "?")}s -> ${text.take(40)}")
                text
            }
        } catch (e: Exception) {
            lastError = when {
                e.message?.contains("UnknownHost") == true -> "连不上 api.minimaxi.com，检查手机网络"
                e.message?.contains("timeout", ignoreCase = true) == true -> "识别超时，再试一次"
                else -> "识别失败：${e.message?.take(120)}"
            }
            Log.w(TAG, "ASR 失败", e)
            null
        }
    }

    /** 最近一次失败原因（给用户看的一句话）。 */
    @Volatile
    var lastError: String = ""
        private set

    companion object {
        private const val TAG = "Stt"
    }
}
