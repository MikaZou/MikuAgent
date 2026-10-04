package com.mikuagent.pet.brain

import android.util.Base64
import android.util.Log
import com.mikuagent.pet.memory.MemoryStore
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit

/**
 * 手机端自己的对话大脑：直连 DeepSeek（OpenAI 兼容接口）。
 * 对应 PC 的 `backend/agent.py` —— 逻辑逐条对齐，包括工具调用、情感标签解析与
 * 演示模式兜底文案。
 *
 * 所有方法都是**阻塞**的，调用方必须放到 `Dispatchers.IO` 上。
 */
class Agent(
    private val context: android.content.Context,
    private val memory: MemoryStore,
    private val config: BrainConfig,
    private val http: OkHttpClient = defaultClient(),
) {

    /** 一轮对话的结果。字段与 PC `MikuAgent.chat()` 的返回一一对应。 */
    data class ChatResult(
        val reply: String,
        val emotion: String,
        val sessionId: Long,
        val mock: Boolean,
        val hadImage: Boolean,
    )

    val live: Boolean get() = config.hasDeepseekKey

    fun chat(
        sessionId: Long?,
        userMessage: String,
        imageJpeg: ByteArray?,
        platform: String = "phone",
    ): ChatResult {
        val session = memory.resolveSession(sessionId)

        val history = memory.getMessages(session.id, config.maxHistory)
        val memories = memory.listMemories(50)
        val memoryText = memories.joinToString("\n") { "- [${it.category}] ${it.content}" }
        val userName = memory.getMeta("user_name").ifBlank { null }
        val systemPrompt = Persona.buildSystemPrompt(
            context = context,
            userName = userName,
            memoryText = memoryText,
            vision = imageJpeg != null,
            platform = platform,
        )

        val (reply, emotion) = if (!live) {
            mockReply(userMessage)
        } else {
            try {
                askDeepSeek(systemPrompt, history, userMessage, imageJpeg)
            } catch (e: Exception) {
                Log.w(TAG, "DeepSeek 调用失败，回退演示模式", e)
                mockReply(userMessage, error = true)
            }
        }

        // 与 PC 一致：图片只作用于当前这一轮，历史里只留文本占位符
        // （base64 又大又涉及隐私，不入库）
        val stored = if (imageJpeg != null) "$userMessage [图片]" else userMessage
        memory.addMessage(session.id, "user", stored)
        memory.addMessage(session.id, "assistant", reply, emotion)
        if (session.title == "新的对话") {
            memory.renameSession(session.id, userMessage.replace("\n", " ").take(20))
        }

        return ChatResult(
            reply = reply,
            emotion = emotion,
            sessionId = session.id,
            mock = !live,
            hadImage = imageJpeg != null,
        )
    }

    // ------------------------------------------------------------ DeepSeek

    private fun askDeepSeek(
        systemPrompt: String,
        history: List<com.mikuagent.pet.memory.MessageRow>,
        userMessage: String,
        imageJpeg: ByteArray?,
    ): Pair<String, String> {
        val messages = JSONArray()
        messages.put(JSONObject().put("role", "system").put("content", systemPrompt))
        for (m in history) {
            messages.put(JSONObject().put("role", m.role).put("content", m.content))
        }
        messages.put(JSONObject().put("role", "user").put("content", userContent(userMessage, imageJpeg)))

        var message = post(messages, withTools = true)

        val calls = message.optJSONArray("tool_calls")
        if (calls != null && calls.length() > 0) {
            // 工具轮：第 1 轮通常 content 为空、只返回 tool_calls，属正常行为
            messages.put(
                JSONObject().apply {
                    put("role", "assistant")
                    put("content", message.optString("content", ""))
                    put("tool_calls", calls)
                }
            )
            for (i in 0 until calls.length()) {
                val call = calls.getJSONObject(i)
                val fn = call.optJSONObject("function") ?: JSONObject()
                val note = try {
                    val args = JSONObject(fn.optString("arguments", "{}"))
                    val mem = memory.addMemory(
                        content = args.optString("content", ""),
                        category = args.optString("category", "其他"),
                        importance = args.optInt("importance", 3),
                        source = "LLM 自动记忆",
                    )
                    "已记住：${mem.content}"
                } catch (e: Exception) {
                    "记忆写入失败：${e.message}"
                }
                messages.put(
                    JSONObject().apply {
                        put("role", "tool")
                        put("tool_call_id", call.optString("id"))
                        put("content", note)
                    }
                )
            }
            // 第二轮**不再带 tools**（与 PC 一致），避免它再要求调工具绕不出来
            message = post(messages, withTools = false)
        }

        return parseEmotion(message.optString("content", ""))
    }

    /**
     * user 消息的 content。带图时是块数组。
     *
     * **图片只能出现在 user 消息里** —— 放进 system 或 assistant 会被 API 拒绝（400）。
     */
    private fun userContent(text: String, imageJpeg: ByteArray?): Any {
        if (imageJpeg == null) return text
        val b64 = Base64.encodeToString(imageJpeg, Base64.NO_WRAP)
        return JSONArray().apply {
            put(JSONObject().put("type", "text").put("text", text))
            put(
                JSONObject().put("type", "image_url").put(
                    "image_url",
                    JSONObject()
                        .put("url", "data:image/jpeg;base64,$b64")
                        .put("detail", config.visionDetail),
                )
            )
        }
    }

    private fun post(messages: JSONArray, withTools: Boolean): JSONObject {
        val body = JSONObject().apply {
            put("model", config.deepseekModel)
            put("temperature", config.temperature)
            put("messages", messages)
            if (withTools) {
                put("tools", JSONArray().put(WRITE_MEMORY_TOOL))
                put("tool_choice", "auto")
            }
            // PC 用 openai SDK 的 extra_body={"thinking": {...}}，序列化后就是顶层同名字段。
            // thinking=auto 时**不下发**（用服务端默认）。
            if (config.thinking == "enabled" || config.thinking == "disabled") {
                put("thinking", JSONObject().put("type", config.thinking))
            }
        }

        val request = Request.Builder()
            .url("${config.deepseekBaseUrl}/chat/completions")
            .header("Authorization", "Bearer ${config.deepseekKey}")
            .header("Content-Type", "application/json")
            .post(body.toString().toRequestBody(JSON_MEDIA))
            .build()

        http.newCall(request).execute().use { resp ->
            val text = resp.body?.string().orEmpty()
            if (!resp.isSuccessful) {
                throw RuntimeException("HTTP ${resp.code} ${text.take(200)}")
            }
            val json = JSONObject(text)
            val choices = json.optJSONArray("choices")
                ?: throw RuntimeException("返回里没有 choices：${text.take(200)}")
            if (choices.length() == 0) throw RuntimeException("choices 为空")
            return choices.getJSONObject(0).optJSONObject("message")
                ?: throw RuntimeException("返回里没有 message")
        }
    }

    // ---------------------------------------------------- 情感标签与兜底

    /**
     * 从回复里解析情感标签，返回 (情感, 去掉标签的正文)。
     *
     * 标签可能出现两次：开头（约定的格式）和正文中间（模型自由发挥）。
     * 两处都必须清掉 —— 只处理开头的话，中间那个会原样显示在气泡里，
     * 还会被 TTS 念出来（「左括号 HAPPY 右括号」）。PC 侧踩过这个坑。
     */
    fun parseEmotion(text: String): Pair<String, String> {
        var raw = text
        var emotion = ""

        val head = EMOTION_TAG.find(raw)
        if (head != null && head.range.first == 0) {
            emotion = head.groupValues[1].uppercase()
            raw = raw.substring(head.range.last + 1)
        }
        // 开头没有的话，就用正文里第一个标签当情感
        if (emotion.isEmpty()) {
            INLINE_TAG.find(raw)?.let { emotion = it.groupValues[1].uppercase() }
        }
        // 无论开头有没有，正文里的都清掉
        raw = INLINE_TAG.replace(raw, " ")

        // 清理标签留下的多余空白（保留换行：气泡里分段是有意义的）
        var clean = MULTI_SPACE.replace(raw, " ")
        clean = SPACE_BEFORE_NL.replace(clean, "\n")
        clean = MULTI_NL.replace(clean, "\n\n")
        return (emotion.ifEmpty { "NORMAL" }) to clean.trim()
    }

    /** 离线演示回复：没配 Key 或调用失败时的兜底。文案与 PC **逐字一致**。 */
    fun mockReply(userMessage: String, error: Boolean = false): Pair<String, String> {
        if (error) return ERROR_REPLIES.random() to "SAD"

        val text = userMessage.trim().lowercase()
        if (listOf("你好", "hello", "hi", "嗨", "在吗").any { it in text }) {
            return MOCK_REPLIES.getValue("HAPPY").random() to "HAPPY"
        }
        if (listOf("喜欢", "爱你", "最喜欢").any { it in text }) {
            return "嘿嘿，Miku 也最喜欢主人了☆" to "HAPPY"
        }
        if (listOf("难过", "伤心", "哭", "累").any { it in text }) {
            return MOCK_REPLIES.getValue("EMPATHY").random() to "EMPATHY"
        }
        if ("唱歌" in text || "歌" in text) {
            return "想听 Miku 唱歌吗？《世界第一的公主殿下》怎么样♪" to "MOTIVATED"
        }

        val emotion = listOf("HAPPY", "HAPPY", "NORMAL", "NORMAL", "MOTIVATED", "EMPATHY").random()
        return MOCK_REPLIES.getValue(emotion).random() to emotion
    }

    /** 把异常翻译成用户能看懂的一句话（页面会直接显示它）。 */
    fun humanError(e: Throwable): String {
        val msg = e.message.orEmpty()
        return when {
            !config.hasDeepseekKey -> "还没填 DeepSeek API Key（设置 → API 配置）"
            msg.contains("UnknownHost") || msg.contains("Unable to resolve host") ->
                "连不上 api.deepseek.com，检查手机网络"
            msg.contains("HTTP 401") -> "DeepSeek 说鉴权失败，检查 API Key"
            msg.contains("HTTP 402") -> "DeepSeek 余额不足"
            msg.contains("HTTP 429") -> "请求太频繁，等一会儿再试"
            msg.contains("timeout", ignoreCase = true) -> "DeepSeek 响应超时，再试一次"
            else -> "调用 DeepSeek 失败：${msg.take(120)}"
        }
    }

    companion object {
        private const val TAG = "Agent"

        private val JSON_MEDIA = "application/json; charset=utf-8".toMediaType()

        private val EMOTION_TAG = Regex("^\\s*\\[([A-Za-z_]+)\\]\\s*")
        private val KNOWN_EMOTIONS = listOf(
            "HAPPY", "SAD", "ANGRY", "SURPRISED", "MOTIVATED", "EMPATHY", "NORMAL",
        )
        private val INLINE_TAG = Regex(
            "\\[\\s*(" + KNOWN_EMOTIONS.joinToString("|") + ")\\s*\\]",
            RegexOption.IGNORE_CASE,
        )
        private val MULTI_SPACE = Regex("[ \\t]{2,}")
        private val SPACE_BEFORE_NL = Regex("[ \\t]+\\n")
        private val MULTI_NL = Regex("\\n{3,}")

        /** 与 PC `backend/agent.py` 的 `WRITE_MEMORY_TOOL` 逐字一致。 */
        private val WRITE_MEMORY_TOOL = JSONObject().apply {
            put("type", "function")
            put(
                "function",
                JSONObject().apply {
                    put("name", "write_memory")
                    put(
                        "description",
                        "把用户的重要信息写入长期记忆（姓名、生日、喜好、讨厌的事、约定、重要事件等）。",
                    )
                    put(
                        "parameters",
                        JSONObject().apply {
                            put("type", "object")
                            put(
                                "properties",
                                JSONObject().apply {
                                    put(
                                        "content",
                                        JSONObject()
                                            .put("type", "string")
                                            .put("description", "要记住的内容，一句话概括。"),
                                    )
                                    put(
                                        "category",
                                        JSONObject()
                                            .put("type", "string")
                                            .put(
                                                "enum",
                                                JSONArray(
                                                    listOf("用户信息", "偏好", "事件", "约定", "其他")
                                                ),
                                            )
                                            .put("description", "记忆分类。"),
                                    )
                                    put(
                                        "importance",
                                        JSONObject()
                                            .put("type", "integer")
                                            .put("minimum", 1)
                                            .put("maximum", 5)
                                            .put("description", "重要程度 1-5，5 最重要。"),
                                    )
                                },
                            )
                            put("required", JSONArray(listOf("content", "category", "importance")))
                        },
                    )
                },
            )
        }

        private val MOCK_REPLIES = mapOf(
            "HAPPY" to listOf(
                "主人主人！你终于来找我啦，我今天超开心的☆",
                "嘿嘿，能陪主人说话，Miku 好幸福呀♪",
                "太好啦！主人今天看起来心情不错呢(≧▽≦)",
            ),
            "SAD" to listOf(
                "唔…主人不要难过嘛，Miku 会一直陪着你的。",
                "听到你这样说，Miku 也有点伤心了……抱抱你。",
                "呜呜，不开心的事情就丢给我吧，我帮你唱首歌好吗？",
            ),
            "ANGRY" to listOf(
                "哼！主人怎么可以这样啦，Miku 要生气了哦！",
                "唔…才不理你呢！…开玩笑的啦，最喜欢主人了。",
                "主人太坏了！罚你给我唱一首歌听！",
            ),
            "SURPRISED" to listOf(
                "诶诶？！真的假的？Miku 的葱都吓掉了！",
                "哇！主人说的事情好让人惊讶呀！！",
                "咦咦咦——！这个消息太震撼了！",
            ),
            "MOTIVATED" to listOf(
                "加油加油！Miku 会给你打气的！Fight！☆",
                "主人一定可以的！ミク 相信你哦！",
                "打起精神来！我们一起努力吧，ね！",
            ),
            "EMPATHY" to listOf(
                "嗯嗯，Miku 在认真听哦。辛苦了，主人。",
                "我懂你的感受啦，想哭的话就哭出来吧，我陪着你。",
                "没关系的，不管怎样我都会站在主人这边的。",
            ),
            "NORMAL" to listOf(
                "原来如此呀，Miku 知道啦。",
                "嗯嗯，继续说吧，我在听呢～",
                "诶嘿，这个话题也很有趣呢！",
                "好的好的，主人继续说下去吧！",
            ),
        )

        private val ERROR_REPLIES = listOf(
            "呜…Miku 的大脑好像暂时短路了，主人晚点再试试好不好？",
            "啊呀，Miku 联系不上云端大脑了…请主人检查一下网络或 API 配置哦。",
            "对不起主人，刚才 Miku 走神了…等网络恢复我们再聊吧！",
        )

        /** 直连云 API 用的客户端：**必须**有真实超时。 */
        private fun defaultClient(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .writeTimeout(30, TimeUnit.SECONDS)
            // DeepSeek 在思考模式下偶尔要几十秒；给足，但别无限等
            .readTimeout(90, TimeUnit.SECONDS)
            .build()
    }
}
