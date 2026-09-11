"""诊断待机动画：呼吸参数有没有在动、Idle 动作有多长、动作结束后状态如何。"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

import live2d.v3 as live2d
from PySide6.QtCore import Qt, QTimer
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QApplication

MODEL = BASE / "assets" / "live2d" / "miku" / "miku.model3.json"
MOTION_DIR = MODEL.parent / "表情和动作"

# ---- 静态信息：Idle 动作的时长 ----
print("=== Idle 动作组的文件与时长 ===")
try:
    spec = json.loads(MODEL.read_text(encoding="utf-8"))
    for m in spec["FileReferences"]["Motions"].get("Idle", []):
        p = MODEL.parent / m["File"]
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
            dur = d.get("Meta", {}).get("Duration", "?")
            loops = d.get("Meta", {}).get("Loop", "?")
            # 统计动了哪些参数
            params = sorted({t["Id"] for mt in d.get("Curves", [])
                             for t in [{"Id": mt.get("Id")}] if mt.get("Id")})
            print(f"  {m['File']}")
            print(f"     时长 {dur}s  循环={loops}  参数数 {len(params)}")
            print(f"     参数: {', '.join(params[:8])}{' ...' if len(params) > 8 else ''}")
        except Exception as e:
            print(f"  {m['File']}  读取失败: {e}")
except Exception as e:
    print("  解析 model3.json 失败:", e)


class Probe(QOpenGLWidget):
    def __init__(self):
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.resize(360, 600)
        self.m = None
        self.samples = []
        self.n = 0

    def initializeGL(self):
        live2d.glInit()
        self.m = live2d.LAppModel()
        self.m.LoadModelJson(str(MODEL))
        self.m.SetAutoBlinkEnable(True)
        self.m.SetAutoBreathEnable(True)
        # 找呼吸参数
        self.param_ids = []
        try:
            for i in range(self.m.GetParameterCount()):
                self.param_ids.append(self.m.GetParameter(i).id)
        except Exception:
            pass
        breath = [p for p in self.param_ids if "Breath" in p]
        print(f"\n=== 参数检查 ===")
        print(f"  参数总数: {len(self.param_ids)}")
        print(f"  含 Breath 的参数: {breath if breath else '（没有！自动呼吸无从作用）'}")
        for probe in ("ParamAngleX", "ParamAngleY", "ParamAngleZ", "ParamBodyAngleX",
                      "ParamEyeLOpen", "ParamMouthOpenY", "ParamBreath"):
            print(f"    {probe}: {'有' if probe in self.param_ids else '无'}")
        self.startTimer(16)

    def paintGL(self):
        live2d.clearBuffer(0, 0, 0, 0)
        if self.m:
            self.m.Update()
            self.m.Draw()

    def timerEvent(self, e):
        self.n += 1
        if self.m and self.n % 6 == 0 and len(self.samples) < 60:
            row = {}
            for pid in ("ParamBreath", "ParamAngleX", "ParamAngleY", "ParamEyeLOpen"):
                if pid in self.param_ids:
                    try:
                        row[pid] = round(float(self.m.GetParameterValue(pid)), 3)
                    except Exception:
                        row[pid] = None
            self.samples.append(row)
        self.update()


def main() -> int:
    live2d.init()
    app = QApplication([])
    w = Probe()
    w.show()
    QTimer.singleShot(3500, app.quit)
    app.exec()

    print("\n=== 待机时参数随时间变化（自动眨眼+自动呼吸，未播任何动作）===")
    for pid in ("ParamBreath", "ParamAngleX", "ParamAngleY", "ParamEyeLOpen"):
        vals = [s.get(pid) for s in w.samples if s.get(pid) is not None]
        if vals:
            uniq = len(set(vals))
            print(f"  {pid:16s} 采样{len(vals):3d} 个  不同值 {uniq:3d}  "
                  f"范围 [{min(vals):.3f}, {max(vals):.3f}]  "
                  f"{'← 在动' if uniq > 3 else '← 基本不动'}")
        else:
            print(f"  {pid:16s} 无数据")
    live2d.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
