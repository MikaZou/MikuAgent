package com.mikuagent.pet.brain

/**
 * 送进 TTS 之前的文本清洗与分句。逐条移植 `backend/tts.py` 的
 * `normalize_text` / `split_sentences` —— 这两步直接决定「听起来自不自然」，
 * 两端必须一致，否则同一句话在手机和 PC 上念出来不一样。
 */
object TtsText {

    /** 白名单：CJK 汉字、CJK 标点、全角字符、ASCII 可打印。 */
    private val ALLOWED = Regex("[^\u3000-\u303F\u4E00-\u9FFF\uFF00-\uFFEF\u0020-\u007E]")
    private val CJK = Regex("[\u4E00-\u9FFF]")
    private val TAG_SQUARE = Regex("\\[[^\\]]{0,24}\\]")
    private val TAG_CJK = Regex("【[^】]{0,24}】")
    private val KAOMOJI = Regex("[\\(\uFF08][^\\)\uFF09]{0,24}[\\)\uFF09]")
    private val ELLIPSIS = Regex("…+|。{2,}|\\.{3,}")
    private val TILDE = Regex("[~\uFF5E]+")
    private val COMMA_RUN = Regex("[，,]{2,}")
    private val LEAD_PUNCT = Regex("^[\\s，,、]+")
    private val TAIL_PUNCT = Regex("[\\s，,、]+$")
    private val WS_RUN = Regex("\\s{2,}")
    private val HAS_CONTENT = Regex("[\u4E00-\u9FFFA-Za-z0-9]")

    /**
     * 把带颜文字/emoji/装饰符号的回复清洗成可自然朗读的文本。
     *
     * 人设会输出 ☆ ♪ (≧▽≦) （´▽｀）…… 这类内容，直接丢给 TTS 会读出怪音或杂音。
     * 保留句末的 。！？ —— 它们对语调有帮助；省略号/波浪号转成逗号做停顿。
     */
    fun normalize(text: String?): String {
        if (text.isNullOrEmpty()) return ""
        var out = text

        // 1) 去掉残留的情感标签（正常已被 Agent.parseEmotion 剥掉，这里兜底）
        out = TAG_SQUARE.replace(out, "")
        out = TAG_CJK.replace(out, "")

        // 2) 颜文字：括号内不含汉字就整体删掉，避免留下「（｀）」这种碎片
        out = KAOMOJI.replace(out) { m ->
            if (CJK.containsMatchIn(m.value)) m.value else ""
        }

        // 3) 省略号 / 波浪号 → 停顿（比直接删掉自然）
        out = ELLIPSIS.replace(out, "，")
        out = TILDE.replace(out, "，")

        // 4) 白名单过滤掉 emoji、假名、装饰符号
        out = ALLOWED.replace(out, "")

        // 5) 收尾规整
        out = COMMA_RUN.replace(out, "，")
        out = LEAD_PUNCT.replace(out, "")
        out = TAIL_PUNCT.replace(out, "")
        out = WS_RUN.replace(out, " ")

        // 6) 清洗后若只剩标点（例如整句都是日文假名被过滤掉），视为无内容
        if (!HAS_CONTENT.containsMatchIn(out)) return ""
        return out.trim()
    }

    private val SENT_END = charArrayOf('。', '！', '？', '!', '?', '；', ';', '\n')

    /**
     * 把回复切成适合逐段合成的片段。
     *
     * 云端 TTS 是「整段合成完才出声」，长回复要等好几秒。按句切开逐段合成/播放，
     * 第一句合成完就能开口，首字延迟大幅下降。过短的碎片并入前一段，
     * 避免碎成一堆词。
     */
    fun splitSentences(text: String?, maxLen: Int = 40): List<String> {
        if (text.isNullOrEmpty()) return emptyList()

        // Python 的 re.split 带 lookbehind，分隔符留在前一段末尾；这里手工等价实现
        val raw = mutableListOf<String>()
        var start = 0
        for (i in text.indices) {
            if (text[i] in SENT_END) {
                raw += text.substring(start, i + 1)
                start = i + 1
            }
        }
        if (start < text.length) raw += text.substring(start)

        val parts = raw.map { it.trim() }.filter { it.isNotEmpty() }
        if (parts.isEmpty()) return emptyList()

        val out = mutableListOf<String>()
        for (original in parts) {
            var part = original
            // 过长的句子再按逗号切一刀
            while (part.length > maxLen) {
                val window = part.substring(0, maxLen)
                val cut = maxOf(window.lastIndexOf('，'), window.lastIndexOf(','))
                if (cut <= 0) break
                out += part.substring(0, cut + 1)
                part = part.substring(cut + 1)
            }
            if (out.isNotEmpty() && part.length < 4) {
                out[out.size - 1] = out.last() + part
            } else {
                out += part
            }
        }
        return out.filter { it.isNotBlank() }
    }

    // ---------------------------------------------------------- 语速补偿表
    // 以下都是**实测标定值**，不要自己重调（改了听感就不对了）。

    /** 情感 → MiniMax 的 voice_setting.emotion（官方取值见 /v1/t2a_v2）。 */
    val MINIMAX_EMOTION = mapOf(
        "HAPPY" to "happy",
        "SAD" to "sad",
        "ANGRY" to "angry",
        "SURPRISED" to "surprised",
        "MOTIVATED" to "happy",
        "EMPATHY" to "neutral",
        "NORMAL" to "neutral",
    )

    /** 没配克隆音色时的兜底系统音色（少女音）。 */
    const val MINIMAX_FALLBACK_VOICE = "female-shaonv"

    /**
     * MiniMax 各 emotion 的实际语速倍率（实测标定，去静音后以 neutral = 1.0）。
     * 同一个 speed 参数下 surprised/happy 比 neutral 快 23~29%、angry 快约 17%。
     */
    val MINIMAX_EMOTION_RATE = mapOf(
        "neutral" to 1.000,
        "sad" to 0.929,
        "angry" to 1.174,
        "happy" to 1.233,
        "surprised" to 1.286,
    )

    /** 语速 ∝ speed^1.19（实测）。补偿要用 (比值)^(1/指数)，直接用 1/比值会补偿过头。 */
    const val MINIMAX_SPEED_EXPONENT = 1.19

    val MINIMAX_SPEED_RANGE = 0.5 to 2.0

    /** 情绪 → 期望的相对语速（1.00 = NORMAL 基准）。幅度控制在 ±12% 以内。 */
    val EMOTION_SPEED_TARGET = mapOf(
        "NORMAL" to 1.00,
        "HAPPY" to 1.08,
        "SAD" to 0.90,
        "ANGRY" to 1.12,
        "SURPRISED" to 1.06,
        "MOTIVATED" to 1.10,
        "EMPATHY" to 0.94,
    )

    /** 句式/标点对语速的微调。幅度都压得很小，避免听感上忽快忽慢。 */
    private const val BIAS_EXCLAIM = 1.04
    private const val BIAS_QUESTION = 1.03
    private const val BIAS_ELLIPSIS = 0.94
    private const val BIAS_LONG = 0.95
    private const val BIAS_LONG_CHARS = 30

    private val EXCLAIM = Regex("[！!]")
    private val QUESTION = Regex("[？?]")

    /** 按句式/标点微调语速，返回 (倍率, 触发原因)。 */
    fun contentSpeedBias(text: String): Pair<Double, List<String>> {
        var bias = 1.0
        val reasons = mutableListOf<String>()
        if (EXCLAIM.containsMatchIn(text)) {
            bias *= BIAS_EXCLAIM; reasons += "感叹"
        }
        if (QUESTION.containsMatchIn(text)) {
            bias *= BIAS_QUESTION; reasons += "疑问"
        }
        if ("…" in text || "..." in text) {
            bias *= BIAS_ELLIPSIS; reasons += "省略"
        }
        if (text.length >= BIAS_LONG_CHARS) {
            bias *= BIAS_LONG; reasons += "长句"
        }
        return bias to reasons
    }

    /**
     * 反解出应当下发的 `voice_setting.speed`。
     *
     * 模型：最终语速 ∝ speed^1.19 × MiniMax 自身的情绪倍率。
     * 期望：最终语速 = 基准 × 情绪目标 × 句式微调，
     * 于是 speed = 基准 × (期望倍率 / MiniMax情绪倍率)^(1/1.19)。
     *
     * MiniMax 的 emotion 不只是音色，会连带改变真实语速（surprised 比 neutral
     * 快约 29%）。直接把 speed 设成基准值会得到「有时快有时慢」；
     * 按 1/倍率 补偿又会过头。所以用实测指数反解。
     *
     * @param biasSource 用来判断句式的**原始**文本 —— 归一化会把「……」换成「，」，
     *                   用归一化后的文本判断的话，省略号规则永远不会命中。
     */
    fun minimaxSpeed(
        baseSpeed: Double,
        mmEmotion: String,
        biasSource: String,
        emotionLabel: String,
    ): Double {
        var speed = baseSpeed
        var intended = EMOTION_SPEED_TARGET[emotionLabel.uppercase()] ?: 1.0
        if (biasSource.isNotEmpty()) intended *= contentSpeedBias(biasSource).first
        val mmRate = MINIMAX_EMOTION_RATE[mmEmotion] ?: 1.0
        if (mmRate > 0 && intended > 0) {
            speed *= Math.pow(intended / mmRate, 1.0 / MINIMAX_SPEED_EXPONENT)
        }
        return Math.round(speed.coerceIn(MINIMAX_SPEED_RANGE.first, MINIMAX_SPEED_RANGE.second) * 100.0) / 100.0
    }
}
