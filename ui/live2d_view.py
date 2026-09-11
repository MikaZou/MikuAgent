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
import time
from pathlib import Path
from typing import Optional

import OpenGL.GL as gl
import live2d.v3 as live2d
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

        # 口型
        self._wav: Optional[WavHandler] = None
        self._speak_fallback = False
        self._speak_start = 0.0

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
            # 视线跟随鼠标（窗口内坐标）
            local = self.mapFromGlobal(QCursor.pos())
            self.model.Drag(local.x(), local.y())

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
    def play_motion(self, group: str) -> None:
        if self.model is None or not group:
            return
        try:
            self.model.StartRandomMotion(
                group=group, priority=live2d.MotionPriority.NORMAL
            )
        except Exception:  # noqa: BLE001
            pass

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

    def set_emotion(self, emotion: str) -> None:
        """情感标签 → 动作 + 表情（对齐旧版 setEmotion）。"""
        emotion = (emotion or "NORMAL").upper()
        self.play_motion(EMOTION_MOTION.get(emotion, "Idle"))
        self.set_expression(EMOTION_EXPRESSION.get(emotion))

        # 生气时手动压低眉毛
        if emotion == "ANGRY" and self.model is not None:
            for pid in ("ParamBrowLY", "ParamBrowRY"):
                try:
                    self.model.SetParameterValue(
                        getattr(live2d.StandardParams, pid), -1.0
                    )
                except Exception:  # noqa: BLE001
                    pass

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
