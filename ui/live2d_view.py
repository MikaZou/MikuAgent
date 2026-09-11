"""Live2D 渲染视图（live2d-py / Cubism Native，无 Web Engine）。

职责：
  - 在透明 OpenGL 窗口里渲染 Miku
  - 情感标签 → 动作组 + 表情（语义与旧版 frontend/js/live2d.js 保持一致）
  - 视线跟随、点击互动、拖拽移动窗口
  - 口型同步：优先用 WavHandler 读真实音频包络，无音频时退回正弦模拟
"""
from __future__ import annotations

import math
import os
import random
import time
from pathlib import Path
from typing import Optional

import OpenGL.GL as gl
import live2d.v3 as live2d
import numpy as np
from live2d.utils.lipsync import WavHandler
from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtOpenGLWidgets import QOpenGLWidget

# 情感 → 动作组（对应 miku.model3.json 的 Motions）
EMOTION_MOTION = {
    "HAPPY": "Tap",
    "ANGRY": "Flick",
    "SURPRISED": "FlickUp",
    "SAD": "Cry",
    "MOTIVATED": "Tap",
    "EMPATHY": "Idle",
    "NORMAL": "Idle",
}

# 情感 → 表情（对应「表情和动作/」下的 exp3）
#
# 注意：模型自带的 Dazhihui 与 Mimiyan 两个 exp3 用在这个模型上会画出错位的
# 红色横条（压在眼睛上）和翻白眼，实测逐个应用后确认是坏素材，已弃用。
# 只用验证过正常的三张：Chijing（吃惊）、Saihong（腮红）、liuhan（泪眼）。
EMOTION_EXPRESSION = {
    "HAPPY": "Saihong",      # 腮红
    "MOTIVATED": "Saihong",  # 元气也用腮红（原 Dazhihui 会画出错位红条）
    "SURPRISED": "Chijing",  # 吃惊
    "EMPATHY": "liuhan",     # 温柔/共情用泪眼（原 Mimiyan 会翻白眼）
    "SAD": "liuhan",         # 难过用泪眼
    "ANGRY": None,           # 生气用动作表现
    "NORMAL": None,
}

# 点击时随机播放的动作池（对齐旧版 live2d.js）
CLICK_MOTIONS = ["Tap", "Tap", "Flick", "FlickUp", "Dance", "Idle"]


class Live2DView(QOpenGLWidget):
    """透明置顶的 Live2D 画布，同时充当桌宠主窗口。"""

    model_clicked = Signal()
    model_load_failed = Signal(str)

    def __init__(
        self,
        model_path: Path,
        fps: int = 60,
        lipsync_gain: float = 1.6,
        framing_scale: float = 1.0,
        framing_offset: tuple[float, float] = (0.0, 0.0),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.model_path = Path(model_path)
        self.fps = max(10, min(144, fps))
        self.lipsync_gain = lipsync_gain
        self.framing_scale = framing_scale
        self.framing_offset = framing_offset

        self.model: Optional[live2d.LAppModel] = None
        self.expression_ids: list[str] = []
        self.motion_groups: dict = {}
        self.param_ids: list[str] = []

        # 口型
        self._wav: Optional[WavHandler] = None
        self._speak_fallback = False
        self._speak_start = 0.0

        # 待机动画
        # 之前只有眨眼在动：动作只在「收到回复」和「点击」时播一次，
        # 播完就停在默认姿态，没有任何东西持续驱动模型。
        self.idle_enabled = True
        self.idle_group = "Idle"
        self.idle_gap_range = (2.5, 7.0)   # 两次待机动作之间的随机间隔（秒）
        self._next_idle_at = 0.0           # 0 = 启动后立刻来一个
        self._breath_t0 = time.time()

        # 交互
        self._pressed_in_model = False
        self._drag_origin = QPoint()
        self._win_origin = QPoint()
        self._mouse_in_model = False

        self.scale_factor = QGuiApplication.primaryScreen().devicePixelRatio()

        # 透明 + 无边框 + 置顶 + 不进任务栏
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 注意：这里**不能**设 WA_AlwaysStackOnTop。
        # 它会让 GL 内容无视正常层叠顺序、永远画在最上面，
        # 结果气泡 / 角标按钮这些子控件反而被模型盖住（已实测确认：
        # 设了它之后，模型区域内的子控件像素会被替换成模型颜色）。
        # 逐像素透明只需要 WA_TranslucentBackground。
        self.setMouseTracking(True)
        self.setWindowTitle("MikuAgent")
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------ OpenGL
    def initializeGL(self) -> None:
        live2d.glInit()

        # 先自己检查文件是否存在：Cubism 的 LoadModelJson 在文件缺失时
        # 不是抛异常而是**直接卡死**，必须提前拦住。
        if not self.model_path.exists():
            self.model = None
            self.model_load_failed.emit(f"模型文件不存在：{self.model_path}")
            return

        try:
            self.model = live2d.LAppModel()
            self.model.LoadModelJson(str(self.model_path))
            self.model.SetAutoBlinkEnable(True)
            self.model.SetAutoBreathEnable(True)
            self._apply_framing()
        except Exception as exc:  # noqa: BLE001
            # 模型/驱动有问题时不要让整个程序崩掉：置空模型并通知上层回退到静态立绘
            print(f"[Live2D] 模型加载失败：{exc}")
            self.model = None
            self.model_load_failed.emit(str(exc))
            return

        # 有些方法在封装层里并不存在（例如 GetPartCount），这里逐个容错
        try:
            self.expression_ids = list(self.model.GetExpressionIds())
        except Exception:  # noqa: BLE001
            self.expression_ids = []
        try:
            self.motion_groups = dict(self.model.GetMotionGroups())
        except Exception:  # noqa: BLE001
            self.motion_groups = {}
        # 参数表：本模型用大写 PARAM_* 命名，与 Cubism 标准名（ParamAngleX 等）
        # 不一致，很多「标准名」写法会静默失效，所以要先拿到真实 ID 列表。
        try:
            self.param_ids = [
                self.model.GetParameter(i).id
                for i in range(self.model.GetParameterCount())
            ]
        except Exception:  # noqa: BLE001
            self.param_ids = []

        self.startTimer(int(1000 / self.fps))

    def resizeGL(self, w: int, h: int) -> None:
        if self.model is not None:
            self.model.Resize(w, h)
            self._apply_framing()

    def _apply_framing(self) -> None:
        """整体缩放与偏移，用于给气泡留出头顶空间。"""
        if self.model is None:
            return
        try:
            self.model.SetScale(self.framing_scale)
            self.model.SetOffset(*self.framing_offset)
        except Exception:  # noqa: BLE001
            pass

    def set_framing(self, scale: float, offset: tuple[float, float]) -> None:
        self.framing_scale = scale
        self.framing_offset = offset
        self._apply_framing()
        self.update()

    def paintGL(self) -> None:
        live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)  # 全透明
        if self.model is None:
            return
        self.model.Update()
        self.model.Draw()

    # --------------------------------------------------------------- 帧循环
    def timerEvent(self, event) -> None:  # noqa: N802
        if not self.isVisible():
            return

        if self.model is not None:
            now = time.time()

            # 视线跟随鼠标（窗口内坐标）
            local = self.mapFromGlobal(QCursor.pos())
            self.model.Drag(local.x(), local.y())

            # 待机动画：动作播完就接下一个，否则模型会一直僵在默认姿态
            self._update_idle(now)
            self._update_breath(now)

            if self._wav is not None:
                if self._wav.Update():
                    value = min(1.0, max(0.0, self._wav.GetRms() * self.lipsync_gain))
                    self.model.SetParameterValue(
                        live2d.StandardParams.ParamMouthOpenY, value
                    )
                else:
                    self.stop_lipsync()
            elif self._speak_fallback:
                elapsed = (time.time() - self._speak_start) * 1000.0
                value = max(
                    0.0,
                    min(
                        1.0,
                        (math.sin(elapsed * 0.012)
                         + math.sin(elapsed * 0.031) * 0.4
                         + 0.8) * 0.5,
                    ),
                )
                self.model.SetParameterValue(
                    live2d.StandardParams.ParamMouthOpenY, value
                )

        self.update()

    # ------------------------------------------------------------ 表情 / 动作
    def play_motion(self, group: str, priority: Optional[int] = None) -> None:
        if self.model is None or not group:
            return
        try:
            self.model.StartRandomMotion(
                group=group,
                priority=priority if priority is not None else live2d.MotionPriority.NORMAL,
            )
        except Exception:  # noqa: BLE001
            pass

    def _pick_idle_motion(self) -> Optional[int]:
        """在 Idle 组里加权挑一个动作序号。

        实测本模型 Idle 组有 3 个：07_点头(1.3s)、14_点头(2.9s)、09_渐入睡眠(21s)。
        均匀随机的话有 1/3 概率进 21 秒的睡眠动作，又会长时间看着不动。
        这里用 1/(1+i) 让靠前的（较短的）动作权重大，睡眠动作降到约 18%。
        """
        try:
            count = int(self.motion_groups.get(self.idle_group, 0) or 0)
        except Exception:  # noqa: BLE001
            count = 0
        if count <= 0:
            return None
        weights = [1.0 / (1 + i) for i in range(count)]
        return random.choices(range(count), weights=weights, k=1)[0]

    def _update_idle(self, now: float) -> None:
        """待机调度：当前动作播完后，隔一小段随机时间再接一个 Idle 动作。

        这是「看着僵」的根因修复 —— 原来动作只在收到回复和点击时各播一次，
        播完就停在默认姿态。另外实测这个模型的 motion3.json 虽然写了
        Loop=true，运行时 IsMotionFinished() 仍会在几秒后变 True，并不循环。
        """
        if not self.idle_enabled or self.model is None:
            return
        try:
            if not self.model.IsMotionFinished():
                return              # 正忙着（回复动作 / 点击动作），别抢
        except Exception:  # noqa: BLE001
            return
        if now < self._next_idle_at:
            return
        index = self._pick_idle_motion()
        if index is None:
            self.play_motion(self.idle_group, priority=live2d.MotionPriority.IDLE)
        else:
            try:
                self.model.StartMotion(
                    self.idle_group, index, live2d.MotionPriority.IDLE
                )
            except Exception:  # noqa: BLE001
                self.play_motion(self.idle_group, priority=live2d.MotionPriority.IDLE)
        lo, hi = self.idle_gap_range
        self._next_idle_at = now + random.uniform(lo, hi)

    def _update_breath(self, now: float) -> None:
        """手动驱动呼吸。

        这个模型的参数叫 PARAM_BREATH（大写），而 SetAutoBreathEnable() 只认
        标准名 ParamBreath —— 所以本模型上自动呼吸是**完全失效**的，必须自己驱动。
        实测 Idle 动作本身并不写 PARAM_BREATH（一直停在 0），所以这里无条件驱动；
        若将来某个动作真的写了它，动作会在 Update() 里覆盖本值，也不会冲突。
        """
        if self.model is None:
            return
        # PARAM_BREATH 取值 0~1；频率约 0.22Hz，接近真人静息呼吸
        phase = (math.sin((now - self._breath_t0) * 1.4) + 1.0) * 0.5
        self.set_param(phase, "PARAM_BREATH", "ParamBreath")

    def set_expression(self, name: Optional[str]) -> None:
        if self.model is None:
            return
        try:
            # 先清掉上一张：否则两张 exp3 的参数会叠加，出现「表情重叠」
            self.model.ResetExpression()
            if name and name in self.expression_ids:
                self.model.SetExpression(name)
        except Exception:  # noqa: BLE001
            pass

    def resolve_param(self, *candidates: str) -> Optional[str]:
        """按候选顺序返回第一个真实存在的参数 ID。

        本模型用大写 PARAM_* 命名，和 Cubism 标准名不一致，
        直接写标准名（如 ParamBrowLY）会静默失效 —— 传 None 也不会报错，
        只是那行代码什么都不做，很难发现。
        """
        for name in candidates:
            if name in self.param_ids:
                return name
        return None

    def set_param(self, value: float, *candidates: str) -> bool:
        """按候选名设置参数，成功返回 True。"""
        pid = self.resolve_param(*candidates)
        if pid is None or self.model is None:
            return False
        try:
            self.model.SetParameterValue(pid, value)
            return True
        except Exception:  # noqa: BLE001
            return False

    def set_emotion(self, emotion: str) -> None:
        """情感标签 → 动作 + 表情（对齐旧版 setEmotion）。"""
        emotion = (emotion or "NORMAL").upper()
        self.play_motion(EMOTION_MOTION.get(emotion, "Idle"))
        self.set_expression(EMOTION_EXPRESSION.get(emotion))

        # 生气时手动压低眉毛。
        # 注意要兼容两种命名，原来写的 ParamBrowLY / ParamBrowRY 在本模型上
        # 根本不存在，这行代码一直是空转的。
        if emotion == "ANGRY":
            for cands in (("PARAM_BROW_L_Y", "ParamBrowLY"),
                          ("PARAM_BROW_R_Y", "ParamBrowRY")):
                self.set_param(-1.0, *cands)

    # -------------------------------------------------------------- 口型同步
    def start_lipsync(self, wav_path: Optional[str]) -> None:
        """开始说话。wav_path 有效 → 真实口型；否则退回正弦模拟。"""
        self.stop_lipsync()
        if wav_path and os.path.exists(wav_path):
            try:
                handler = WavHandler()
                handler.Start(str(wav_path))
                self._wav = handler
                return
            except Exception:  # noqa: BLE001
                self._wav = None
        self._speak_fallback = True
        self._speak_start = time.time()

    def stop_lipsync(self) -> None:
        self._wav = None
        self._speak_fallback = False
        if self.model is not None:
            try:
                self.model.SetParameterValue(
                    live2d.StandardParams.ParamMouthOpenY, 0.0
                )
            except Exception:  # noqa: BLE001
                pass

    @property
    def speaking(self) -> bool:
        return self._wav is not None or self._speak_fallback

    # ------------------------------------------------------------- 命中检测
    def _alpha_at(self, x: float, y: float) -> int:
        """读取 framebuffer 指定点的 alpha，用于按像素命中检测。"""
        w, h = self.width(), self.height()
        px = int(x * self.scale_factor)
        py = int((h - y) * self.scale_factor)
        if px < 0 or py < 0 or px >= int(w * self.scale_factor) or py >= int(h * self.scale_factor):
            return 0
        try:
            data = gl.glReadPixels(px, py, 1, 1, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
        except Exception:  # noqa: BLE001
            return 0
        return data[3] if data else 0

    def is_in_model(self, x: float, y: float) -> bool:
        return self._alpha_at(x, y) > 8

    def measure_model_top(self) -> Optional[int]:
        """测出模型最上面一行在窗口内的 y（逻辑像素，原点在左上）。

        一次性读整块 framebuffer 的 alpha 通道取最小行，比逐点 glReadPixels 快得多。
        外层用它把角标按钮贴在初音头顶上方，而不是固定在窗口顶端留一大片空白。
        注意 glReadPixels 原点在左下，所以要翻转。
        """
        if self.model is None:
            return None
        w = int(self.width() * self.scale_factor)
        h = int(self.height() * self.scale_factor)
        if w <= 0 or h <= 0:
            return None
        try:
            self.makeCurrent()
            try:
                raw = gl.glReadPixels(0, 0, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            finally:
                self.doneCurrent()
        except Exception:  # noqa: BLE001
            return None
        if not raw:
            return None
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size < w * h * 4:
            return None
        alpha = arr.reshape(h, w, 4)[:, :, 3]
        rows = np.where(alpha.max(axis=1) > 8)[0]
        if len(rows) == 0:
            return None
        top_device = int(rows.max())          # GL 坐标：行号越大越靠上
        return int((h - 1 - top_device) / self.scale_factor)

    # ------------------------------------------------------------------ 鼠标
    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return
        pos = event.position()
        if self.is_in_model(pos.x(), pos.y()):
            self._pressed_in_model = True
            self._drag_origin = event.globalPosition().toPoint()
            self._win_origin = self.pos()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._pressed_in_model:
            delta = event.globalPosition().toPoint() - self._drag_origin
            self.move(self._win_origin + delta)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._pressed_in_model:
            self._pressed_in_model = False
            # 没怎么移动 → 视为点击互动
            moved = (event.globalPosition().toPoint() - self._drag_origin).manhattanLength()
            if moved < 6:
                self.click_interaction()

    def click_interaction(self) -> None:
        """点击 Miku：随机动作 + 冒个泡（由外部接管气泡）。"""
        import random

        self.play_motion(random.choice(CLICK_MOTIONS))
        self.model_clicked.emit()

    # ------------------------------------------------------------------ 清理
    def shutdown(self) -> None:
        self.stop_lipsync()
        self.model = None
