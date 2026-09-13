"""DeepSeek Agent：对话大脑 + 情感标签解析 + 长期记忆写入。"""
import base64
import json
import random
import re
from typing import Optional

from openai import OpenAI

import config
from memory import MemoryStore
from persona import build_system_prompt

EMOTION_TAG = re.compile(r"^\s*\[([A-Za-z_]+)\]\s*")
# 正文中间也可能冒出标签（实测模型写过「…吗～？ [HAPPY] 当然可以呀！」）。
# 只清「已知情感名」的方括号，避免误伤正文里像 [1] 这样的正常方括号。
KNOWN_EMOTIONS = (
    "HAPPY", "SAD", "ANGRY", "SURPRISED", "MOTIVATED", "EMPATHY", "NORMAL",
)
INLINE_EMOTION_TAG = re.compile(
    r"\[\s*(" + "|".join(KNOWN_EMOTIONS) + r")\s*\]", re.IGNORECASE
)

WRITE_MEMORY_TOOL = {
    "type": "function",
    "function": {
        "name": "write_memory",
        "description": (
            "把用户的重要信息写入长期记忆（姓名、生日、喜好、讨厌的事、约定、重要事件等）。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容，一句话概括。"},
                "category": {
                    "type": "string",
                    "enum": ["用户信息", "偏好", "事件", "约定", "其他"],
                    "description": "记忆分类。",
                },
                "importance": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 5,
                    "description": "重要程度 1-5，5 最重要。",
                },
            },
            "required": ["content", "category", "importance"],
        },
    },
}

MOCK_REPLIES = {
    "HAPPY": [
        "主人主人！你终于来找我啦，我今天超开心的☆",
        "嘿嘿，能陪主人说话，Miku 好幸福呀♪",
        "太好啦！主人今天看起来心情不错呢(≧▽≦)",
    ],
    "SAD": [
        "唔…主人不要难过嘛，Miku 会一直陪着你的。",
        "听到你这样说，Miku 也有点伤心了……抱抱你。",
        "呜呜，不开心的事情就丢给我吧，我帮你唱首歌好吗？",
    ],
    "ANGRY": [
        "哼！主人怎么可以这样啦，Miku 要生气了哦！",
        "唔…才不理你呢！…开玩笑的啦，最喜欢主人了。",
        "主人太坏了！罚你给我唱一首歌听！",
    ],
    "SURPRISED": [
        "诶诶？！真的假的？Miku 的葱都吓掉了！",
        "哇！主人说的事情好让人惊讶呀！！",
        "咦咦咦——！这个消息太震撼了！",
    ],
    "MOTIVATED": [
        "加油加油！Miku 会给你打气的！Fight！☆",
        "主人一定可以的！ミク 相信你哦！",
        "打起精神来！我们一起努力吧，ね！",
    ],
    "EMPATHY": [
        "嗯嗯，Miku 在认真听哦。辛苦了，主人。",
        "我懂你的感受啦，想哭的话就哭出来吧，我陪着你。",
        "没关系的，不管怎样我都会站在主人这边的。",
    ],
    "NORMAL": [
        "原来如此呀，Miku 知道啦。",
        "嗯嗯，继续说吧，我在听呢～",
        "诶嘿，这个话题也很有趣呢！",
        "好的好的，主人继续说下去吧！",
    ],
}

ERROR_REPLIES = [
    "呜…Miku 的大脑好像暂时短路了，主人晚点再试试好不好？",
    "啊呀，Miku 联系不上云端大脑了…请主人检查一下网络或 API 配置哦。",
    "对不起主人，刚才 Miku 走神了…等网络恢复我们再聊吧！",
]


def parse_emotion(text: str) -> tuple[str, str]:
    """从回复中解析情感标签，返回 (情感, 去除标签后的正文)。

    标签可能出现两次：开头（约定的格式）和正文中间（模型自由发挥）。
    两处都必须清掉 —— 只处理开头的话，中间那个会原样显示在气泡里，
    还会被 TTS 念出来（「左括号 HAPPY 右括号」）。
    """
    raw = text or ""

    emotion = ""
    match = EMOTION_TAG.match(raw)
    if match:
        emotion = match.group(1).upper()
        raw = raw[match.end():]

    # 开头没有的话，就用正文里第一个标签当情感
    if not emotion:
        found = INLINE_EMOTION_TAG.search(raw)
        if found:
            emotion = found.group(1).upper()
    # 无论开头有没有，正文里的都清掉
    raw = INLINE_EMOTION_TAG.sub(" ", raw)

    # 清理标签留下的多余空白（保留换行：气泡里分段是有意义的）
    clean = re.sub(r"[ \t]{2,}", " ", raw)
    clean = re.sub(r"[ \t]+\n", "\n", clean)
    clean = re.sub(r"\n{3,}", "\n\n", clean)
    return (emotion or "NORMAL"), clean.strip()


class MikuAgent:
    """初音未来 Agent：负责与 DeepSeek 对话、管理记忆。"""

    def __init__(self, memory: MemoryStore):
        self.memory = memory
        self.model = config.DEEPSEEK_MODEL
        self.temperature = config.DEEPSEEK_TEMPERATURE
        self.client = (
            OpenAI(api_key=config.DEEPSEEK_API_KEY, base_url=config.DEEPSEEK_BASE_URL)
            if config.HAS_API_KEY
            else None
        )

    @property
    def live(self) -> bool:
        return self.client is not None and not config.MOCK_MODE

    # ------------------------------------------------------------ 请求构造
    def _base_kwargs(self) -> dict:
        """公共请求参数：模型、温度、思考模式。

        DEEPSEEK_THINKING=auto 时不下发该参数（用服务端默认）。
        注意：思考模式下 temperature 会被静默忽略，所以桌宠默认 disabled。
        """
        kw: dict = {"model": self.model, "temperature": self.temperature}
        if config.DEEPSEEK_THINKING in ("enabled", "disabled"):
            kw["extra_body"] = {"thinking": {"type": config.DEEPSEEK_THINKING}}
        return kw

    @staticmethod
    def _user_content(text: str, image: Optional[bytes]):
        """构造 user 消息的 content。

        带图时返回块数组。**图片只能出现在 user 消息里** —— 放进 system
        或 assistant 会被 API 拒绝（400）。
        """
        if not image:
            return text
        b64 = base64.b64encode(image).decode("ascii")
        return [
            {"type": "text", "text": text},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:image/jpeg;base64,{b64}",
                    "detail": config.VISION_DETAIL,
                },
            },
        ]

    def chat(
        self,
        session_id: Optional[int],
        user_message: str,
        image: Optional[bytes] = None,
        platform: str = "pc",
    ) -> dict:
        """处理一轮对话，返回 {reply, emotion, session_id, mock, session_title}。

        image 为 JPEG 字节时走多模态；图片只作用于**当前这一轮**，
        历史里只留文本占位符（不落盘、不入库）。

        platform 为 "phone" 或 "pc"，决定提示词里「怎么让你看到画面」怎么写
        （两端的按钮不一样，写错她会指挥用户去点不存在的按钮）。

        **会话按天划分，三端共享同一条**（见 `MemoryStore.resolve_session`）。
        `session_id` 在这里只是「同一天内的偏好」：它若不属于今天 —— 客户端跨零点
        还缓存着昨天的、或桌面端连续运行过了午夜 —— 会被换成今天的会话。
        跨天纠正只发生在 memory 层，Android / 网页 / 桌面客户端都不用自己判断日期。
        """
        session = self.memory.resolve_session(session_id)

        history = self.memory.get_messages(session["id"], limit=config.MAX_HISTORY_MESSAGES)
        memories = self.memory.list_memories(limit=50)
        memory_text = "\n".join(
            f"- [{m['category']}] {m['content']}" for m in memories
        )
        user_name = self.memory.get_meta("user_name") or None
        system_prompt = build_system_prompt(
            user_name=user_name,
            memory_text=memory_text,
            vision=bool(image),
            platform=platform,
        )

        if not self.live:
            reply_text, emotion = self._mock_reply(user_message)
        else:
            try:
                reply_text, emotion = self._ask_deepseek(
                    system_prompt, history, user_message, image
                )
            except Exception as exc:
                print(f"[MikuAgent] DeepSeek 调用失败，回退演示模式: {exc}")
                reply_text, emotion = self._mock_reply(user_message, error=True)

        # 历史只存文本 + 占位符：图片是 base64 大字符串，入库既臃肿又涉及隐私
        stored = f"{user_message} [图片]" if image else user_message
        self.memory.add_message(session["id"], "user", stored)
        self.memory.add_message(
            session["id"], "assistant", reply_text, emotion=emotion
        )
        if session["title"] == "新的对话":
            self.memory.rename_session(
                session["id"], user_message.strip().replace("\n", " ")[:20]
            )

        return {
            "reply": reply_text,
            "emotion": emotion,
            "session_id": session["id"],
            "mock": not self.live,
            "had_image": bool(image),
        }

    def _ask_deepseek(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
        image: Optional[bytes] = None,
    ) -> tuple[str, str]:
        """调用 DeepSeek chat API，支持 write_memory 工具调用与图片输入。"""
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append(
            {"role": "user", "content": self._user_content(user_message, image)}
        )

        response = self.client.chat.completions.create(
            messages=messages,
            tools=[WRITE_MEMORY_TOOL],
            tool_choice="auto",
            **self._base_kwargs(),
        )
        message = response.choices[0].message

        if message.tool_calls:
            # 工具轮：第 1 轮通常 content 为空、只返回 tool_calls，属正常行为
            messages.append(message.model_dump(exclude_none=True))
            for call in message.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                    mem = self.memory.add_memory(
                        content=str(args.get("content", "")),
                        category=str(args.get("category", "其他")),
                        importance=int(args.get("importance", 3)),
                        source="LLM 自动记忆",
                    )
                    note = f"已记住：{mem['content']}"
                except Exception as exc:
                    note = f"记忆写入失败：{exc}"
                messages.append(
                    {"role": "tool", "tool_call_id": call.id, "content": note}
                )
            response = self.client.chat.completions.create(
                messages=messages, **self._base_kwargs()
            )
            message = response.choices[0].message

        emotion, clean = parse_emotion(message.content or "")
        return clean, emotion

    def _mock_reply(self, user_message: str, error: bool = False) -> tuple[str, str]:
        """离线演示回复：无 API Key 或调用失败时的兜底。"""
        if error:
            return random.choice(ERROR_REPLIES), "SAD"

        text = user_message.strip().lower()
        if any(k in text for k in ("你好", "hello", "hi", "嗨", "在吗")):
            return random.choice(MOCK_REPLIES["HAPPY"]), "HAPPY"
        if any(k in text for k in ("喜欢", "爱你", "最喜欢")):
            return "嘿嘿，Miku 也最喜欢主人了☆", "HAPPY"
        if any(k in text for k in ("难过", "伤心", "哭", "累")):
            return random.choice(MOCK_REPLIES["EMPATHY"]), "EMPATHY"
        if any(k in text for k in ("唱歌", "歌")):
            return "想听 Miku 唱歌吗？《世界第一的公主殿下》怎么样♪", "MOTIVATED"

        emotion = random.choice(
            ["HAPPY", "HAPPY", "NORMAL", "NORMAL", "MOTIVATED", "EMPATHY"]
        )
        return random.choice(MOCK_REPLIES[emotion]), emotion
