"""回归：合并规则必须与 `shared/sync_rules.json` 契约逐条一致。

手机端 `MergeRulesTest.kt` 读**同一个**契约文件。两边都对着它跑，
就不会出现「两端各自都觉得自己对」的静默分歧。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

import sync_rules  # noqa: E402

RULES = BASE_DIR / "shared" / "sync_rules.json"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


def main() -> int:
    contract = json.loads(RULES.read_text(encoding="utf-8"))

    # ---- 1) decide ----
    for case in contract["decide"]:
        local = case["local"]
        local_ts = None if local is None else float(local["updated_at"])
        got = sync_rules.decide(local_ts, float(case["incoming"]["updated_at"]))
        check(f"decide: {case['name']}", got == case["expect"],
              f"期望 {case['expect']} 实得 {got}")
    check("decide 覆盖了三种可能的结果",
          {c["expect"] for c in contract["decide"]} == {"insert", "update", "keep"},
          str(sorted({c["expect"] for c in contract["decide"]})))

    # ---- 2) pick_primary ----
    for case in contract["pick_primary"]:
        got = sync_rules.pick_primary(case["candidates"])
        check(f"pick_primary: {case['name']}", got == case["expect"],
              f"期望 {case['expect']} 实得 {got}")

    # ---- 3) day_of ----
    for case in contract["day_of"]:
        got = sync_rules.day_of(case["created_at"])
        check(f"day_of({case['created_at']!r})", got == case["expect"],
              f"期望 {case['expect']!r} 实得 {got!r}")

    # ---- 4) rewind ----
    for case in contract["rewind"]:
        got = sync_rules.rewind(case["watermark"])
        check(f"rewind({case['watermark']})", abs(got - case["expect"]) < 1e-9,
              f"期望 {case['expect']} 实得 {got}")

    # ---- 5) 常量 ----
    check("游标回退窗口 = 300 秒（时钟偏差靠它兜底）",
          sync_rules.watermark_overlap_seconds() == 300.0,
          str(sync_rules.watermark_overlap_seconds()))
    check("单次拉取上限合理（2000 条 / 手机一屏同步不该卡）",
          0 < sync_rules.plan_limit() <= 5000, str(sync_rules.plan_limit()))
    check("推送分片不超过拉取上限",
          0 < sync_rules.push_chunk() <= sync_rules.plan_limit(),
          str(sync_rules.push_chunk()))

    # ---- 6) 幂等：同一批来料重复应用，结果必须收敛 ----
    # 这是「回退 300 秒重扫」能成立的前提。
    rows: dict[str, float] = {}
    incoming = [("a", 100.0), ("b", 200.0), ("c", 150.0)]
    for _ in range(3):
        for uuid, ts in incoming:
            if sync_rules.decide(rows.get(uuid), ts) != sync_rules.KEEP:
                rows[uuid] = ts
    check("重复应用同一批来料结果不变", rows == {"a": 100.0, "b": 200.0, "c": 150.0}, str(rows))

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
