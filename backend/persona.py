"""初音未来（Hatsune Miku）角色设定：渲染**共享**的提示词模板。

文案只有一份：`shared/persona.txt`。PC（本模块）与手机（Kotlin 的
`brain/Persona.kt`）读同一个文件、按同一套规则渲染 —— 否则「人设」就变成两份，
改一次只改到一边，模型行为会莫名其妙地分叉。

## 模板语法（极小，两端都只有这一个规则）

    {{#名字}} … {{/名字}}    可选区块；关闭时**整段（含两个标记）删掉**
    {占位符}                 必填值，直接替换

区块的标记写在**行内**（不是独占一行），这样换行数完全由模板文本决定，
渲染器不需要对空行做任何「聪明」的修补。

## 为什么有这些区块，而不是让模板自己排版

原实现是把块 append 进 `parts` 列表再 `"\\n".join`，于是不同组合下空行数
并不一致 —— 最典型的是「有长期记忆」时记忆正文后面跟着**三个**换行。
现在的模板把每个区块的换行原样写进文件里（`{{#memory}}` 后面跟两个换行、
`{{/memory}}` 前面留一个换行），所以渲染结果与原来**逐字相同**。

`tools/test_persona_parity.py` 用 32 个组合把结果钉死在
`tools/persona_golden.json`；Kotlin 侧的单测读同一个文件比对。
改这里之前先看那个测试 —— 一个空格变了它就会红。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Optional

TEMPLATE_PATH = Path(__file__).resolve().parent.parent / "shared" / "persona.txt"

# 非贪婪匹配 + 反向引用闭合标签名，避免 {{#a}}…{{/b}} 被错配
_SECTION = re.compile(r"\{\{#(\w+)\}\}(.*?)\{\{/\1\}\}", re.DOTALL)

_cache: Optional[str] = None


def template_text() -> str:
    """读模板（去掉文件结尾那一个换行，它是文本文件惯例而不是提示词的一部分）。"""
    global _cache
    if _cache is None:
        if not TEMPLATE_PATH.exists():
            raise FileNotFoundError(f"提示词模板缺失：{TEMPLATE_PATH}")
        body = TEMPLATE_PATH.read_text(encoding="utf-8")
        if body.endswith("\n"):
            body = body[:-1]
        _cache = body
    return _cache


def reset_cache() -> None:
    """测试用：改过模板文件之后强制重读。"""
    global _cache
    _cache = None


def render(
    sections: Optional[dict] = None,
    values: Optional[dict] = None,
) -> str:
    """渲染模板。区块先处理、占位符后处理。

    顺序不能反：占位符的值里万一出现 `{{#…}}` 就会被当成区块解析；
    而区块删掉之后，它内部的占位符自然也不用再替换了。
    """
    sections = sections or {}
    values = values or {}

    def _pick(match: re.Match) -> str:
        return match.group(2) if sections.get(match.group(1)) else ""

    out = _SECTION.sub(_pick, template_text())
    for key, value in values.items():
        out = out.replace("{" + key + "}", value)
    return out


def build_system_prompt(
    user_name: Optional[str] = None,
    memory_text: str = "",
    extra_note: str = "",
    vision: bool = False,
    platform: str = "pc",
) -> str:
    """根据人设、用户昵称与长期记忆，构建系统提示词。

    vision=True 时追加本次画面的视觉规则；「关于看见」那一段则**始终注入**，
    用于防止「上一轮有图、这一轮没图」时的幻觉（详见模板里那一段的说明）。

    platform 决定「怎么让你看到画面」这句怎么写 —— PC 和手机的按钮不一样，
    写错了 Miku 就会指挥用户去点不存在的按钮。

    签名与输出与重构前**完全一致**（靠 tools/test_persona_parity.py 保证）。
    """
    is_phone = platform == "phone"
    return render(
        sections={
            "user": bool(user_name),
            # 判断与插入都用 strip 后的值（与原实现一致）
            "memory": bool(memory_text.strip()),
            "vision": bool(vision),
            "note": bool(extra_note.strip()),
            "hint_pc": not is_phone,
            "hint_phone": is_phone,
        },
        values={
            "USER_NAME": user_name or "",
            "MEMORY_TEXT": memory_text.strip(),
            "NOTE_TEXT": extra_note.strip(),
        },
    )
