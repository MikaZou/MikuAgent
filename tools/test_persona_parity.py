"""回归：人设提示词的渲染结果必须逐字稳定。

为什么要专门钉住它

提示词现在要**同时**被 PC（`backend/persona.py`）和手机（Kotlin 侧 `Persona.kt`）
使用。文案因此只能有一份 —— 放在 `shared/persona.txt`，两端都读它。
但「两份实现渲染同一个模板」意味着任何一边的换行/空行处理出错，都会变成
一个**很难发现的**人设漂移（模型行为变了，但没人会怀疑到空行上）。

所以这里把渲染结果钉成 golden：`shared/persona_golden.json`。
* PC 侧：本脚本比对；
* 手机侧：`android/app/src/test/.../PersonaTest.kt` 读同一个文件比对
  （跨语言比对，杜绝「两边各写一半」）。

用法：
    .venv\\Scripts\\python.exe tools/test_persona_parity.py            # 比对
    .venv\\Scripts\\python.exe tools/test_persona_parity.py --update   # 重建 golden
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR / "backend"))

GOLDEN = BASE_DIR / "tools" / "persona_golden.json"
TEMPLATE = BASE_DIR / "shared" / "persona.txt"

FAILED: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"  [{'ok' if ok else 'FAIL'}] {name}{('  ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILED.append(name)


def cases() -> list[dict]:
    """覆盖所有会影响渲染的分支（数量少，穷举即可）。"""
    out = []
    for user_name in ("", "主人"):
        for memory in ("", "- [偏好] 喜欢葱"):
            for vision in (False, True):
                for platform in ("pc", "phone"):
                    for note in ("", "【额外】测试备注"):
                        out.append({
                            "user_name": user_name,
                            "memory_text": memory,
                            "vision": vision,
                            "platform": platform,
                            "extra_note": note,
                        })
    return out


def key(case: dict) -> str:
    return "|".join([
        f"user={case['user_name'] or '-'}",
        f"mem={case['memory_text'] or '-'}",
        f"vision={int(case['vision'])}",
        case["platform"],
        f"note={case['extra_note'] or '-'}",
    ])


def render_all() -> list[dict]:
    """每个用例带上自己的输入与期望输出。

    刻意不用 `{key: text}` 那种紧凑形状：Kotlin 侧的单测也要读这个文件，
    而把 "user=主人|mem=-|…" 这样的 key 再解析回字段又脆又难看。
    """
    from persona import build_system_prompt

    out = []
    for c in cases():
        out.append({
            **c,
            "expected": build_system_prompt(
                user_name=c["user_name"] or None,
                memory_text=c["memory_text"],
                extra_note=c["extra_note"],
                vision=c["vision"],
                platform=c["platform"],
            ),
        })
    return out


def main() -> int:
    update = "--update" in sys.argv

    check("模板文件存在", TEMPLATE.exists(), str(TEMPLATE.relative_to(BASE_DIR)))
    if TEMPLATE.exists():
        import re

        body = TEMPLATE.read_text(encoding="utf-8")
        # 占位符写错名字不会报错，只会原样留在提示词里发给人设 —— 必须静态查一遍。
        # 两个集合都要和 persona.py / Persona.kt 支持的保持一致：
        known_values = {"USER_NAME", "MEMORY_TEXT", "NOTE_TEXT"}
        known_sections = {"user", "memory", "vision", "note", "hint_pc", "hint_phone"}

        found_values = set(re.findall(r"\{([A-Z_]+)\}", body))
        unknown_values = sorted(found_values - known_values)
        check("模板的 {{占位符}} 都是认得的", not unknown_values, f"多出来：{unknown_values}")
        check("模板用到了全部 3 个占位符", found_values == known_values, str(sorted(found_values)))

        found_sections = set(re.findall(r"\{\{#(\w+)\}\}", body))
        closed = set(re.findall(r"\{\{/(\w+)\}\}", body))
        check("模板的区块都是认得的",
              not (found_sections - known_sections) and not (closed - known_sections),
              f"多出来：{sorted((found_sections | closed) - known_sections)}")
        check("模板区块开闭配对", found_sections == closed,
              f"开={sorted(found_sections)} 闭={sorted(closed)}")
        check("模板用到了全部 6 个区块", found_sections == known_sections,
              str(sorted(found_sections)))

    rendered = render_all()
    template_sha1 = hashlib.sha1(TEMPLATE.read_bytes()).hexdigest() if TEMPLATE.exists() else ""

    if update or not GOLDEN.exists():
        GOLDEN.parent.mkdir(parents=True, exist_ok=True)
        GOLDEN.write_text(
            json.dumps(
                {"template_sha1": template_sha1, "cases": rendered},
                ensure_ascii=False, indent=1,
            ),
            encoding="utf-8",
        )
        print(f"  已写入 golden：{GOLDEN.relative_to(BASE_DIR)}（{len(rendered)} 个用例）")
        print("\n全部通过")
        return 0

    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    check("golden 记录的模板与当前模板一致",
          golden.get("template_sha1") == template_sha1,
          f"golden {golden.get('template_sha1', '?')[:8]} / 现在 {template_sha1[:8]}"
          "（改过 shared/persona.txt 就要 --update 重建）")

    want = {key(c): c["expected"] for c in golden["cases"]}
    got = {key(c): c["expected"] for c in rendered}
    check("用例集合一致", set(want) == set(got), f"golden {len(want)} / 现在 {len(got)}")

    diffs = 0
    for k in sorted(set(want) & set(got)):
        if want[k] != got[k]:
            diffs += 1
            if diffs <= 3:
                g, r = want[k], got[k]
                # 指出第一处差异的位置，比整段 diff 好读
                i = next((i for i in range(min(len(g), len(r))) if g[i] != r[i]),
                         min(len(g), len(r)))
                print(f"\n  ✗ {k}")
                print(f"    golden[{i}]={g[i:i + 40]!r}")
                print(f"    现在  [{i}]={r[i:i + 40]!r}")
    check(f"{len(want)} 个用例逐字一致", diffs == 0, f"有 {diffs} 个不同")

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
