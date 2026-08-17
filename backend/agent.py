"""DeepSeek Agent：对话大脑 + 情感标签解析 + 长期记忆写入。"""
import json
import random
import re
from typing import Optional

from openai import OpenAI

import config
from memory import MemoryStore
from persona import build_system_prompt

EMOTION_TAG = re.compile(r"^\s*\[([A-Za-z_]+)\]\s*")

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
    """从回复中解析情感标签，返回 (情感, 去除标签后的正文)。"""
    match = EMOTION_TAG.match(text or "")
    if match:
        emotion = match.group(1).upper()
        clean = text[match.end():].strip()
        return emotion, clean
    return "NORMAL", (text or "").strip()


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

    def chat(self, session_id: Optional[int], user_message: str) -> dict:
        """处理一轮对话，返回 {reply, emotion, session_id, mock, session_title}。"""
        session = self.memory.get_session(session_id) if session_id else None
        if session is None:
            session = self.memory.create_session()

        history = self.memory.get_messages(session["id"], limit=config.MAX_HISTORY_MESSAGES)
        memories = self.memory.list_memories(limit=50)
        memory_text = "\n".join(
            f"- [{m['category']}] {m['content']}" for m in memories
        )
        user_name = self.memory.get_meta("user_name") or None
        system_prompt = build_system_prompt(user_name=user_name, memory_text=memory_text)

        if not self.live:
            reply_text, emotion = self._mock_reply(user_message)
        else:
            try:
                reply_text, emotion = self._ask_deepseek(
                    system_prompt, history, user_message
                )
            except Exception as exc:
                print(f"[MikuAgent] DeepSeek 调用失败，回退演示模式: {exc}")
                reply_text, emotion = self._mock_reply(user_message, error=True)

        self.memory.add_message(session["id"], "user", user_message)
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
        }

    def _ask_deepseek(
        self,
        system_prompt: str,
        history: list[dict],
        user_message: str,
    ) -> tuple[str, str]:
        """调用 DeepSeek chat API，支持 write_memory 工具调用。"""
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for msg in history:
            messages.append({"role": msg["role"], "content": msg["content"]})
        messages.append({"role": "user", "content": user_message})

        response = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=self.temperature,
            tools=[WRITE_MEMORY_TOOL],
            tool_choice="auto",
        )
        message = response.choices[0].message

        if message.tool_calls:
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
                model=self.model,
                messages=messages,
                temperature=self.temperature,
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
