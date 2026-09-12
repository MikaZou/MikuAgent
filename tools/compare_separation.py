"""对比 p=1（现场版）与 p=2（手书版）的人声分离质量。

评判指标：
  * 底噪        —— 人声轨最安静 20% 窗口的电平，越低说明伴奏残留越少
  * 动态范围    —— 响段与静段的落差，越大说明人声与静默区分越明显
  * 间奏衰减    —— 纯伴奏段落相对正常演唱段的衰减量，是「分离干净度」的直接证据
  * 相关性      —— 人声轨与伴奏轨的相关系数，越接近 0 越说明分开了
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import soundfile as sf

BASE = Path(__file__).resolve().parent.parent
AUD = BASE / ".tmp" / "audio"

CASES = {
    "p1 现场/htdemucs": AUD / "sep" / "htdemucs" / "miku_raw",
    "p2 手书/htdemucs": AUD / "sep2" / "htdemucs" / "miku_p2",
    "p2 手书/htdemucs_ft": AUD / "sep3" / "htdemucs_ft" / "miku_p2",
    "v4c 说话": AUD / "sep4" / "htdemucs" / "miku_v4c",
}
MIXES = {
    "p1 现场/htdemucs": AUD / "miku_raw.wav",
    "p2 手书/htdemucs": AUD / "miku_p2.wav",
    "p2 手书/htdemucs_ft": AUD / "miku_p2.wav",
    "v4c 说话": AUD / "miku_v4c.wav",
}
WIN = 0.25


def db(x: float) -> float:
    return 20 * np.log10(max(float(x), 1e-9))


def env(mono: np.ndarray, sr: int) -> np.ndarray:
    w = max(1, int(WIN * sr))
    n = len(mono) // w
    return np.sqrt((mono[: n * w].reshape(n, w) ** 2).mean(axis=1))


def analyse(label: str, folder: Path, mix_path: Path) -> dict:
    voc, sr = sf.read(str(folder / "vocals.wav"), always_2d=True)
    nov, _ = sf.read(str(folder / "no_vocals.wav"), always_2d=True)
    mix, _ = sf.read(str(mix_path), always_2d=True)
    vm, nm, mm = voc.mean(1), nov.mean(1), mix.mean(1)

    e = env(vm, sr)
    q20, q90 = np.percentile(e, 20), np.percentile(e, 90)
    # 只看「人声安静但混音不安静」的窗口 —— 那些就是纯伴奏段
    em = env(mm, sr)
    n = min(len(e), len(em))
    quiet_mask = (e[:n] < q20 * 1.3) & (em[:n] > np.percentile(em[:n], 50))
    atten = db(float(np.median(e[:n][quiet_mask]))) - db(float(q90)) if quiet_mask.sum() > 10 else float("nan")

    seg = min(len(vm), len(nm), sr * 90)
    corr = float(np.corrcoef(vm[:seg], nm[:seg])[0, 1])

    return {
        "时长": len(vm) / sr,
        "混音RMS": db(np.sqrt((mm ** 2).mean())),
        "人声RMS": db(np.sqrt((vm ** 2).mean())),
        "底噪": db(q20),
        "响段": db(q90),
        "动态范围": db(q90) - db(q20),
        "间奏衰减": atten,
        "vocals/no_vocals相关": corr,
        "纯伴奏窗口数": int(quiet_mask.sum()),
    }


def main() -> int:
    rows = {}
    for label, folder in CASES.items():
        if not (folder / "vocals.wav").exists():
            print(f"  跳过 {label}：找不到 {folder}/vocals.wav")
            continue
        rows[label] = analyse(label, folder, MIXES[label])

    if not rows:
        return 1

    keys = list(next(iter(rows.values())).keys())
    labels = list(rows.keys())
    width = 20
    print("=" * (24 + width * len(labels)))
    print("分离质量对比")
    print("=" * (24 + width * len(labels)))
    print("  " + "%-22s" % "指标" + "".join("%*s" % (width, l) for l in labels))
    print("  " + "-" * (22 + width * len(labels)))
    for k in keys:
        line = "  %-22s" % k
        for label in labels:
            v = rows[label][k]
            line += "%*s" % (width, "%.2f" % v if isinstance(v, float) else str(v))
        # 标注最优
        if len(labels) > 1 and k in ("底噪", "vocals/no_vocals相关", "动态范围", "间奏衰减"):
            vals = [rows[l][k] for l in labels]
            pick = min(vals) if k in ("底噪", "vocals/no_vocals相关") else max(vals)
            line += "   ← %s 最优" % labels[vals.index(pick)]
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
