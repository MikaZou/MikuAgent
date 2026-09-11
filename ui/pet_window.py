"""原生桌宠主窗口：Live2D 画布 + 气泡 + 输入栏 + 角标按钮。

窗口本身就是 Live2DView（QOpenGLWidget 作为顶层窗口），
气泡/输入栏/按钮是它的子控件，由 Qt 合成在 OpenGL 内容之上。
"""
from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QPixmap
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

import config
from ui.audio import AudioPlayer
from ui.bubble import SpeechBubble
from ui.chat_worker import ChatWorker, TranscribeWorker, TtsPipelineWorker
from ui.input_bar import InputBar
from ui.live2d_view import Live2DView
from ui.settings_dialog import SettingsDialog
from ui.tray import Tray

# 模型取景。实测：Resize 后模型底部贴着窗口底边，dy 为正会把模型上移
# （0.15 ≈ 33 逻辑像素）。
#
# 这里的数值是用 tools/measure_framing.py 在 400x660 下量出来的，
# 目的是让模型的包围盒完整落在「气泡下沿(184) ~ 输入栏上沿(584)」之间：
#   scale 0.85 / dy 0.15  ->  top 203, bottom 576  （完全避开，且左右居中）
# 窗口高度从 580 提到 660，是因为 580 下**没有任何缩放**能让模型避开气泡。
FRAMING_SCALE = 0.85
FRAMING_OFFSET = (0.0, 0.15)

# 布局尺寸（改这里要同步 tools/measure_framing.py 里的同名常量）
BUBBLE_MARGIN = 16      # 气泡左右留白
BUBBLE_TOP = 46         # 气泡距窗口顶部：必须让开上面那排 30px 高的角标按钮
BUBBLE_MAX_H = 132      # 气泡最大高度；模型按这个上沿来避让

GREETING = "主人你好呀！我是初音ミク☆ 把鼠标移到我身上就能和我说话啦～"

CORNER_QSS = """
QPushButton {
    border: none;
    border-radius: 15px;
    font-size: 14px;
    color: #ffffff;
    background: rgba(20, 26, 38, 140);
}
QPushButton:hover { background: rgba(57, 197, 187, 220); }
"""


class PetWindow(Live2DView):
    def __init__(self, memory, agent, stt, tts, parent: Optional[QWidget] = None) -> None:
        super().__init__(
            config.MODEL_PATH,
            fps=config.WINDOW_FPS,
            framing_scale=FRAMING_SCALE,
            framing_offset=FRAMING_OFFSET,
            parent=parent,
        )

        self.memory = memory
        self.agent = agent
        self.stt = stt
        self.tts = tts
        self.audio = AudioPlayer()

        self.session_id: Optional[int] = None
        self._busy = False
        self._hover = False
        self._tts_enabled = SettingsDialog.tts_enabled()
        self._stt_enabled = SettingsDialog.stt_enabled()
        self._chat_worker: Optional[ChatWorker] = None
        self._tts_worker: Optional[TtsWorker] = None
        self._stt_worker: Optional[TranscribeWorker] = None
        self._recording = False

        self.resize(config.WINDOW_WIDTH, config.WINDOW_HEIGHT)

        # ---------------- 子控件 ----------------
        self.bubble = SpeechBubble(self)
        self.bubble.hide()

        self.input_bar = InputBar(self)
        self.input_bar.submitted.connect(self.send_message)
        self.input_bar.hold_started.connect(self.start_voice_input)
        self.input_bar.hold_finished.connect(self.stop_voice_input)
        self.input_bar.hold_cancelled.connect(self.cancel_voice_input)
        self.input_bar.state_changed.connect(self._update_chrome)
        self.input_bar.set_mic_visible(self._stt_enabled)
        self.input_bar.hide()  # 初始隐藏，悬停才出现

        self.btn_min = self._corner_button("─", "最小化", self.showMinimized)
        self.btn_close = self._corner_button("×", "退出桌宠", self.quit_app)
        self.btn_settings = self._corner_button("⚙", "设置", self.open_settings)
        self.btn_close.setStyleSheet(
            CORNER_QSS.replace("rgba(20, 26, 38, 140)", "rgba(239, 68, 68, 200)")
        )

        # ---------------- 托盘 / 设置 ----------------
        self.settings_dialog = SettingsDialog(self)
        self.settings_dialog.nickname_saved.connect(self.save_nickname)
        self.settings_dialog.tts_toggled.connect(self._on_tts_toggled)
        self.settings_dialog.stt_toggled.connect(self._on_stt_toggled)
        self.tray = Tray(self, self.open_settings, self.quit_app)
        self.tray.show()

        self.model_clicked.connect(self._on_model_clicked)
        self.model_load_failed.connect(self._on_model_load_failed)

        self._restore_geometry()
        self._init_session()

    # ------------------------------------------------------------- 角标按钮
    def _corner_button(self, text: str, tip: str, slot) -> QPushButton:
        btn = QPushButton(text, self)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.PointingHandCursor)
        btn.setStyleSheet(CORNER_QSS)
        btn.setFixedSize(30, 30)
        btn.clicked.connect(slot)
        btn.hide()
        return btn

    # ---------------------------------------------------------------- 布局
    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._layout_children()

    def _layout_children(self) -> None:
        w, h = self.width(), self.height()
        self.btn_min.move(10, 10)
        self.btn_close.move(46, 10)
        self.btn_settings.move(w - 40, 10)

        # 气泡：始终占满可用宽度（而不是随文字长短忽宽忽窄），
        # 这样短句也够大、长句换行整齐，不会再被头发挤成一小块。
        bar_h = 58
        bubble_w = max(160, w - BUBBLE_MARGIN * 2)
        self.bubble.setFixedWidth(bubble_w)
        self.bubble.setMaximumHeight(BUBBLE_MAX_H)
        self.bubble.adjustSize()
        self.bubble.move(max(0, (w - self.bubble.width()) // 2), BUBBLE_TOP)

        self.input_bar.setGeometry(12, h - bar_h - 12, w - 24, bar_h)

    # ------------------------------------------------------------ 显示/隐藏
    def enterEvent(self, event) -> None:  # noqa: N802
        self._hover = True
        self._update_chrome()

    def leaveEvent(self, event) -> None:  # noqa: N802
        self._hover = False
        # 延迟一点再判断，给「鼠标从模型移到输入栏」留出余量
        QTimer.singleShot(180, self._update_chrome)

    def _update_chrome(self) -> None:
        # 有焦点或有草稿时钉住输入栏（沿用网页版修好的那套逻辑）
        show_bar = self._hover or self.input_bar.wants_visible
        self.input_bar.setVisible(show_bar)
        buttons = (self.btn_min, self.btn_close, self.btn_settings)
        for btn in buttons:
            btn.setVisible(self._hover and self.isVisible())
        if show_bar:
            self.input_bar.raise_()
        if self.bubble.isVisible():
            self.bubble.raise_()
        # 角标按钮必须最后 raise：气泡是满宽的，早先 bubble.raise_() 会把
        # 左上角的 ─ / × 和右上角的 ⚙ 盖住，点不到也看不见。
        for btn in buttons:
            if btn.isVisible():
                btn.raise_()

    # ---------------------------------------------------------------- 会话
    def _init_session(self) -> None:
        try:
            sessions = self.memory.list_sessions()
            if sessions:
                self.session_id = sessions[0]["id"]
            else:
                self.session_id = self.memory.create_session()["id"]
        except Exception as exc:  # noqa: BLE001
            print(f"[MikuAgent] 会话初始化失败：{exc}")

    # ---------------------------------------------------------------- 对话
    def send_message(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._busy:
            return
        self.stop_speaking()

        self._busy = True
        self.input_bar.set_busy(True)
        self.input_bar.input.clear()
        self.bubble.show_typing()
        self._update_chrome()

        self._chat_worker = ChatWorker(self.agent, self.session_id, text, self)
        self._chat_worker.replied.connect(self._on_reply)
        self._chat_worker.failed.connect(self._on_chat_error)
        self._chat_worker.start()

    def _on_reply(self, result: dict) -> None:
        self.session_id = result.get("session_id")
        reply = result.get("reply", "")
        emotion = result.get("emotion", "NORMAL")

        self.set_emotion(emotion)
        self.bubble.show_message(reply, emotion)
        self._layout_children()
        self._set_busy(False)

        # 语音异步进行，不阻塞输入。走逐句流水线：
        # 第一句合成完就出声，不必等整段回复合成完。
        if self._tts_enabled and self._tts_available():
            self._tts_worker = TtsPipelineWorker(self.tts, reply, emotion, self)
            self._tts_worker.chunk_ready.connect(self._on_tts_ready)
            self._tts_worker.failed.connect(lambda msg: print(f"[TTS] {msg}"))
            self._tts_worker.start()

    def _on_chat_error(self, message: str) -> None:
        print(f"[MikuAgent] 对话失败：{message}")
        self.set_emotion("SAD")
        self.bubble.show_message("呜…Miku 连不上大脑了，检查一下网络或 API Key 好吗？", "SAD")
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self.input_bar.set_busy(busy)
        if not busy:
            self.input_bar.focus_input()

    def _tts_available(self) -> bool:
        return self.tts is not None and self.tts.status == "ready"

    # ---------------------------------------------------------------- 语音输出
    def _on_tts_ready(self, wav_path: str, duration: float) -> None:
        if not wav_path or not self._tts_enabled:
            return
        path = Path(wav_path)
        if not path.exists():
            return
        played = self.audio.play(path)
        if played <= 0:
            return
        self.start_lipsync(str(path))  # 播放与口型读同一份 WAV → 同步

    def stop_speaking(self) -> None:
        # 先停流水线，否则它会在下一句合成完后继续出声
        worker = getattr(self, "_tts_worker", None)
        if worker is not None and worker.isRunning():
            try:
                worker.stop()
                worker.wait(1500)
            except Exception:  # noqa: BLE001
                pass
        self.audio.stop()
        self.stop_lipsync()

    def _on_tts_toggled(self, enabled: bool) -> None:
        self._tts_enabled = enabled
        if not enabled:
            self.stop_speaking()

    def _on_stt_toggled(self, enabled: bool) -> None:
        self._stt_enabled = enabled
        self.input_bar.set_mic_visible(enabled)
        if not enabled and self._recording:
            self.cancel_voice_input()

    # ---------------------------------------------------------------- 语音输入
    def start_voice_input(self) -> None:
        if not self._stt_enabled or self._recording:
            return
        self.stop_speaking()  # 防自激回声
        self._recording = True
        ok, message = self.stt.start()
        if not ok:
            self._recording = False
            self.input_bar.set_recording(False)
            self.bubble.show_message(message or "麦克风不可用，看看是否被占用？", "SAD", 4000)
            return
        self.input_bar.set_recording(True)

    def stop_voice_input(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.input_bar.set_recording(False)
        self._stt_worker = TranscribeWorker(self.stt, self)
        self._stt_worker.transcribed.connect(self._on_transcribed)
        self._stt_worker.start()

    def cancel_voice_input(self) -> None:
        if not self._recording:
            return
        self._recording = False
        self.input_bar.set_recording(False)
        try:
            self.stt.cancel()
        except Exception:  # noqa: BLE001
            pass

    def _on_transcribed(self, result: dict) -> None:
        text = (result.get("text") or "").strip()
        if text:
            self.input_bar.set_text(text)
            self.send_message(text)
        elif result.get("error"):
            self.bubble.show_message(result["error"], "SAD", 4000)

    # ---------------------------------------------------------------- 交互
    def _on_model_load_failed(self, message: str) -> None:
        """Live2D 不可用时回退到静态立绘，保证桌宠仍然可见可用。"""
        print(f"[Live2D] 回退到静态立绘：{message}")
        path = Path(config.AVATAR_PATH)
        if not path.exists():
            self.bubble.show_message("Live2D 加载失败，也找不到备用立绘…", "SAD", 6000)
            return
        pixmap = QPixmap(str(path))
        if pixmap.isNull():
            return
        box_w, box_h = max(80, self.width() - 40), max(80, self.height() - 140)
        self._fallback = QLabel(self)
        self._fallback.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._fallback.setPixmap(
            pixmap.scaled(
                box_w,
                box_h,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
        )
        self._fallback.setGeometry(20, 70, box_w, box_h)
        self._fallback.show()
        self.bubble.show_message("Live2D 加载失败，已切换到图片模式…", "SAD", 6000)

    def _on_model_clicked(self) -> None:
        # 动作已在 Live2DView.click_interaction 里播过，这里只在空闲时冒个泡
        if self._busy or self.bubble.busy_typing:
            return
        if random.random() < 0.35:
            self.bubble.show_message(random.choice([
                "诶嘿，主人戳我做什么呀～",
                "ミク在这里哦！",
                "痒痒的啦！(≧▽≦)",
                "想听我唱歌吗？♪",
            ]), "HAPPY", 3500)

    # ---------------------------------------------------------------- 设置
    def open_settings(self) -> None:
        health = {
            "mock": not getattr(self.agent, "live", False),
            "has_api_key": config.HAS_API_KEY,
            "model": getattr(self.agent, "model", "-"),
            "stt_model": config.STT_MODEL,
            "tts_engine": self.tts.engine if self.tts else "-",
        }
        stt_status = getattr(self.stt, "status", "unknown")
        tts_status = self.tts.status if self.tts else "disabled"
        nickname = ""
        try:
            nickname = self.memory.get_meta("user_name") or ""
        except Exception:  # noqa: BLE001
            pass
        self.settings_dialog.load_state(health, stt_status, tts_status, nickname)
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    def save_nickname(self, name: str) -> None:
        try:
            self.memory.set_meta("user_name", name)
        except Exception as exc:  # noqa: BLE001
            print(f"[MikuAgent] 保存昵称失败：{exc}")
        self.bubble.show_message(
            "记住啦，主人～之后我就这样叫你哦☆" if name else "好的，那我就还是叫你主人啦～",
            "HAPPY",
            4000,
        )

    # ---------------------------------------------------------------- 窗口
    def center_on_screen(self) -> None:
        screen = QApplication.primaryScreen()
        if screen is None:
            return
        geo = screen.availableGeometry()
        self.move(
            geo.center().x() - self.width() // 2,
            geo.center().y() - self.height() // 2,
        )
        self._save_geometry()

    def _restore_geometry(self) -> None:
        path = Path(config.WINDOW_STATE_FILE)
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                self.move(int(data["x"]), int(data["y"]))
                return
            except Exception:  # noqa: BLE001
                pass
        self.center_on_screen()

    def _save_geometry(self) -> None:
        try:
            path = Path(config.WINDOW_STATE_FILE)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"x": self.x(), "y": self.y()}), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._save_geometry()

    # ---------------------------------------------------------------- 生命周期
    def start(self) -> None:
        self.show()
        self.raise_()
        self._update_chrome()
        QTimer.singleShot(700, self._greet)

    def _greet(self) -> None:
        self.set_emotion("HAPPY")
        self.bubble.show_message(GREETING, "HAPPY")
        self._layout_children()
        QTimer.singleShot(9000, self.bubble.hide_bubble)

    def quit_app(self) -> None:
        self.stop_speaking()
        self._save_geometry()
        self.tray.hide()
        self.close()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_speaking()
        self._save_geometry()
        # 注意 self.shutdown() 是 Live2DView 的，收的是渲染。
        # TTS 的合成服务是独立进程，必须单独收掉，否则退出后它会被孤立，
        # 一直占着约 1.5GB 显存不放。
        if getattr(self, "tts", None) is not None:
            try:
                self.tts.shutdown()
            except Exception as exc:  # noqa: BLE001
                print(f"[TTS] 关闭合成服务失败：{exc}")
        self.shutdown()
        super().closeEvent(event)
        # 关掉桌宠窗口就等于退出。app.setQuitOnLastWindowClosed(False) 是为托盘设的，
        # 少了这一句的话，Alt+F4 / 任务栏关闭只会关掉窗口，进程会带着托盘图标
        # 一直留在后台（还会占着显存）。
        QApplication.quit()
