"""双端记忆同步的合并规则（PC 侧）。

**规则本身写在 `shared/sync_rules.json`** —— 手机端 `MergeRules.kt` 实现同一套，
两边的测试读同一个契约文件。这里只是它的 Python 实现。

为什么值得这么麻烦：合并规则两端不一致时，两边各自都「对」，只是一个覆盖了
另一个；表现是「某台设备上的记录悄悄变了」，往往几天后才发现。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

RULES_PATH = Path(__file__).resolve().parent.parent / "shared" / "sync_rules.json"

_cache: Optional[dict] = None


def rules() -> dict:
    global _cache
    if _cache is None:
        _cache = json.loads(RULES_PATH.read_text(encoding="utf-8"))
    return _cache


def watermark_overlap_seconds() -> float:
    return float(rules()["watermark_overlap_seconds"])


def plan_limit() -> int:
    return int(rules()["plan_limit"])


def push_chunk() -> int:
    return int(rules()["push_chunk"])


def max_pull_rounds() -> int:
    return int(rules()["max_pull_rounds"])


# ------------------------------------------------------------------ 单行判定

#: 判定结果。用字符串常量而不是枚举，Kotlin 侧同名同值，便于对照。
INSERT = "insert"
UPDATE = "update"
KEEP = "keep"


def decide(local_updated_at: Optional[float], incoming_updated_at: float) -> str:
    """这条来料该不该落地。

    * 本地没有（None）→ 插入
    * 对方**严格更新** → 覆盖
    * 否则（含时间戳相等）→ 保留本地

    时间戳相等时保留本地是刻意的：这样「同一份数据重复同步」结果不变（幂等），
    而重复同步一定会发生 —— 游标会**回退 300 秒**重扫（见 `rewind`），
    就是为了容忍两台设备的时钟偏差。
    """
    if local_updated_at is None:
        return INSERT
    return UPDATE if incoming_updated_at > local_updated_at else KEEP


# ------------------------------------------------------------ 同日会话取主

def day_of(created_at: str) -> str:
    """会话按「天」分组：`YYYY-MM-DD HH:MM:SS` → `YYYY-MM-DD`。

    这与 PC `MemoryStore.get_or_create_today()` 的 `substr(created_at,1,10)`
    是同一个键 —— 同步两端都按它归并，所以不需要做会话 id ↔ uuid 的映射。
    """
    return (created_at or "")[:10]


def pick_primary(candidates: list[dict]) -> Optional[int]:
    """同一天可能有多条会话（历史碎片）→ 选出消息要并进哪一条。

    规则：消息最多的优先；并列取创建更早的；再并列取 id 最小的。
    必须**完全确定**，否则两端会各自挑一条，消息就永远分在两处。
    """
    if not candidates:
        return None
    best = sorted(
        candidates,
        key=lambda c: (-int(c.get("message_count", 0)),
                       c.get("created_at", ""),
                       int(c["id"])),
    )[0]
    return int(best["id"])


# ------------------------------------------------------------------ 游标

def rewind(watermark: float) -> float:
    """推/拉游标回退一个窗口。

    手机与电脑的时钟不可能完全一致（也没对时）。如果严格用「上次同步时对方的
    时间」当游标，时钟慢的那台设备刚写下的行会被永久跳过。
    回退 300 秒重扫一遍，配合 `decide` 的幂等性，代价只是多传几行。
    """
    return max(0.0, float(watermark) - watermark_overlap_seconds())
