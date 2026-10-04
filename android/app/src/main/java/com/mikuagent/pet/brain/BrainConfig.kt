package com.mikuagent.pet.brain

import android.content.Context
import org.json.JSONObject

/**
 * 手机端「大脑」的全部可配置项。默认值对齐 PC 的 `backend/config.py` ——
 * 两端行为一致，用户换端不该看出差别。
 *
 * 存在 [SecretStore]（加密 prefs）里：里面有 API Key。
 */
data class BrainConfig(
    // ---- DeepSeek（对话）----
    val deepseekKey: String = "",
    val deepseekBaseUrl: String = "https://api.deepseek.com",
    val deepseekModel: String = "deepseek-flash",
    val temperature: Double = 0.9,
    /** enabled / disabled / auto。auto 时不下发该参数，用服务端默认。 */
    val thinking: String = "disabled",

    // ---- MiniMax（语音）----
    val minimaxKey: String = "",
    val minimaxBaseUrl: String = "https://api.minimaxi.com",
    val minimaxTtsModel: String = "speech-2.8-hd",
    val minimaxVoiceId: String = "",
    val minimaxSpeed: Double = 1.8,
    val minimaxAsrModel: String = "asr-1.0",
    val sttLanguage: String = "zh",

    // ---- 其他 ----
    val visionDetail: String = "low",
    val maxHistory: Int = 20,
) {
    /** 与 PC 的 `HAS_API_KEY` 同一个判断：占位符不算配好。 */
    val hasDeepseekKey: Boolean
        get() = deepseekKey.isNotBlank() && !deepseekKey.startsWith("sk-xxx")

    val hasMinimaxKey: Boolean
        get() = minimaxKey.isNotBlank() && !minimaxKey.startsWith("sk-xxx")

    /** 脱敏展示（设置面板用）。 */
    fun masked(key: String): String = when {
        key.isBlank() -> "（未配置）"
        key.length <= 10 -> "已配置"
        else -> "${key.take(6)}…${key.takeLast(4)}"
    }

    fun toJson(): JSONObject = JSONObject().apply {
        put("deepseek_base_url", deepseekBaseUrl)
        put("deepseek_model", deepseekModel)
        put("temperature", temperature)
        put("thinking", thinking)
        put("minimax_base_url", minimaxBaseUrl)
        put("minimax_tts_model", minimaxTtsModel)
        put("minimax_voice_id", minimaxVoiceId)
        put("minimax_speed", minimaxSpeed)
        put("minimax_asr_model", minimaxAsrModel)
        put("stt_language", sttLanguage)
        put("vision_detail", visionDetail)
        put("max_history", maxHistory)
    }

    companion object {
        private const val PREFIX = "brain."

        fun load(context: Context): BrainConfig {
            val p = SecretStore.get(context)
            val d = BrainConfig()
            return BrainConfig(
                deepseekKey = p.getString(PREFIX + "deepseek_key", "") ?: "",
                deepseekBaseUrl = p.getString(PREFIX + "deepseek_base_url", d.deepseekBaseUrl)!!,
                deepseekModel = p.getString(PREFIX + "deepseek_model", d.deepseekModel)!!,
                temperature = p.getString(PREFIX + "temperature", null)?.toDoubleOrNull() ?: d.temperature,
                thinking = p.getString(PREFIX + "thinking", d.thinking)!!,
                minimaxKey = p.getString(PREFIX + "minimax_key", "") ?: "",
                minimaxBaseUrl = p.getString(PREFIX + "minimax_base_url", d.minimaxBaseUrl)!!,
                minimaxTtsModel = p.getString(PREFIX + "minimax_tts_model", d.minimaxTtsModel)!!,
                minimaxVoiceId = p.getString(PREFIX + "minimax_voice_id", "") ?: "",
                minimaxSpeed = p.getString(PREFIX + "minimax_speed", null)?.toDoubleOrNull() ?: d.minimaxSpeed,
                minimaxAsrModel = p.getString(PREFIX + "minimax_asr_model", d.minimaxAsrModel)!!,
                sttLanguage = p.getString(PREFIX + "stt_language", d.sttLanguage)!!,
                visionDetail = p.getString(PREFIX + "vision_detail", d.visionDetail)!!,
                maxHistory = p.getString(PREFIX + "max_history", null)?.toIntOrNull() ?: d.maxHistory,
            )
        }

        /**
         * 保存。传 `null` 的字段**不动**，空串则是「清空」—— 这样「只改模型名」
         * 和「故意把 Key 清掉」可以区分开。
         */
        fun save(
            context: Context,
            deepseekKey: String? = null,
            deepseekBaseUrl: String? = null,
            deepseekModel: String? = null,
            temperature: Double? = null,
            thinking: String? = null,
            minimaxKey: String? = null,
            minimaxBaseUrl: String? = null,
            minimaxTtsModel: String? = null,
            minimaxVoiceId: String? = null,
            minimaxSpeed: Double? = null,
            minimaxAsrModel: String? = null,
            sttLanguage: String? = null,
            visionDetail: String? = null,
            maxHistory: Int? = null,
        ) {
            val e = SecretStore.get(context).edit()
            fun put(key: String, value: Any?) {
                if (value != null) e.putString(PREFIX + key, value.toString())
            }
            put("deepseek_key", deepseekKey?.trim())
            put("deepseek_base_url", deepseekBaseUrl?.trim()?.trimEnd('/'))
            put("deepseek_model", deepseekModel?.trim())
            put("temperature", temperature)
            put("thinking", thinking)
            put("minimax_key", minimaxKey?.trim())
            put("minimax_base_url", minimaxBaseUrl?.trim()?.trimEnd('/'))
            put("minimax_tts_model", minimaxTtsModel?.trim())
            put("minimax_voice_id", minimaxVoiceId?.trim())
            put("minimax_speed", minimaxSpeed)
            put("minimax_asr_model", minimaxAsrModel?.trim())
            put("stt_language", sttLanguage?.trim())
            put("vision_detail", visionDetail?.trim())
            put("max_history", maxHistory)
            e.apply()
        }

        /**
         * 从一段 JSON 批量导入（PC 设置窗口「复制 API 配置」产出的就是它）。
         * 返回实际用到的字段名，便于给用户回显。
         */
        fun importJson(context: Context, json: String): List<String> {
            val o = JSONObject(json)
            val used = mutableListOf<String>()
            fun str(vararg names: String): String? {
                for (n in names) if (o.has(n)) {
                    used += n
                    return o.optString(n, "")
                }
                return null
            }
            val temp = if (o.has("DEEPSEEK_TEMPERATURE")) {
                used += "DEEPSEEK_TEMPERATURE"; o.optDouble("DEEPSEEK_TEMPERATURE")
            } else null
            val speed = if (o.has("MINIMAX_SPEED")) {
                used += "MINIMAX_SPEED"; o.optDouble("MINIMAX_SPEED")
            } else null
            val history = if (o.has("MAX_HISTORY_MESSAGES")) {
                used += "MAX_HISTORY_MESSAGES"; o.optInt("MAX_HISTORY_MESSAGES")
            } else null
            save(
                context,
                deepseekKey = str("DEEPSEEK_API_KEY", "deepseek_key"),
                deepseekBaseUrl = str("DEEPSEEK_BASE_URL", "deepseek_base_url"),
                deepseekModel = str("DEEPSEEK_MODEL", "deepseek_model"),
                temperature = temp,
                thinking = str("DEEPSEEK_THINKING", "thinking"),
                minimaxKey = str("MINIMAX_API_KEY", "minimax_key"),
                minimaxBaseUrl = str("MINIMAX_BASE_URL", "minimax_base_url"),
                minimaxTtsModel = str("MINIMAX_TTS_MODEL", "minimax_tts_model"),
                minimaxVoiceId = str("MINIMAX_VOICE_ID", "minimax_voice_id"),
                minimaxSpeed = speed,
                minimaxAsrModel = str("MINIMAX_ASR_MODEL", "minimax_asr_model"),
                sttLanguage = str("STT_LANGUAGE", "stt_language"),
                visionDetail = str("VISION_DETAIL", "vision_detail"),
                maxHistory = history,
            )
            return used
        }
    }
}
