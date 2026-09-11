"""完整解析模型的动作库：分组、时长、循环、曲线规模，并展示 motion3.json 的结构。"""
from __future__ import annotations

import json
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
MODEL = BASE / "assets" / "live2d" / "miku" / "miku.model3.json"

spec = json.loads(MODEL.read_text(encoding="utf-8"))
motions = spec["FileReferences"]["Motions"]

print("=== 动作总览 ===")
total = 0
rows = []
for group, items in motions.items():
    for i, m in enumerate(items):
        p = MODEL.parent / m["File"]
        d = json.loads(p.read_text(encoding="utf-8"))
        meta = d.get("Meta", {})
        curves = d.get("Curves", [])
        pts = sum(len(c.get("Segments", [])) for c in curves)
        # Segments 是 [t0,v0,t1,v1,...] 的扁平数组（部分类型含贝塞尔控制点）
        params = {c["Id"] for c in curves}
        rows.append((group, i, p.name, meta.get("Duration", 0), meta.get("Loop"),
                     meta.get("FadeInTime"), meta.get("FadeOutTime"),
                     len(curves), pts))
        total += 1

print(f"{'组':<9}{'#':>2} {'文件':<26}{'时长':>7} {'循环':>5} {'淡入':>6} {'淡出':>6} {'曲线':>5} {'控制点':>7}")
print("-" * 78)
for g, i, name, dur, loop, fin, fout, nc, npts in rows:
    print(f"{g:<9}{i:>2} {name:<26}{dur:>6.2f}s {str(loop):>5} "
          f"{('-' if fin is None else fin):>6} {('-' if fout is None else fout):>6} {nc:>5} {npts:>7}")
print(f"\n合计 {total} 个动作，分 {len(motions)} 组")

print("\n=== 各组动作数 ===")
for g, items in motions.items():
    durs = []
    for m in items:
        p = MODEL.parent / m["File"]
        try:
            durs.append(json.loads(p.read_text(encoding="utf-8"))["Meta"]["Duration"])
        except Exception:
            durs.append(0)
    print(f"  {g:<9} {len(items)} 个，时长 {', '.join(f'{d:.2f}s' for d in durs)}")

# 拿一个动作来展示结构
sample = motions["Tap"][0]
sp = MODEL.parent / sample["File"]
sd = json.loads(sp.read_text(encoding="utf-8"))
print(f"\n=== motion3.json 结构示例：{sample['File']} ===")
print(f"  Version : {sd.get('Version')}")
print(f"  Meta    : {json.dumps(sd.get('Meta'), ensure_ascii=False)}")
print(f"  Curves  : {len(sd['Curves'])} 条参数曲线")
for c in sd["Curves"][:4]:
    seg = c["Segments"]
    print(f"    Target={c.get('Target'):<10} Id={c['Id']:<28} 段数={len(seg):>3} 前几个值={seg[:6]}")
