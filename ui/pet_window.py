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
from ui.capture import CameraSession, camera_available, capture_clipboard, capture_screen
from ui.capture_worker import CameraWorker
from ui.chat_worker import ChatWorker, TranscribeWorker, TtsPipelineWorker
from ui.input_bar import InputBar
from ui.live2d_view import Live2DView
from ui.settings_dialog import SettingsDialog
from ui.tray import Tray

# 模型取景。实测：Resize 后模型底部贴着窗口底边，dy 为正会把模型上移
# （0.15 ≈ 33 逻辑像素）。
#
# 这里的数值是用 tools/measure_framing.py 在 360x600 下量出来的，
# 目的是让模型的包围盒完整落在「气泡下沿(184) ~ 输入栏上沿(524)」之间：
#   scale 0.80 / dy 0.15  ->  top 191, bottom 507  （上下各留 7 / 17px）
# 窗口高度不能再减：按钮(40)+气泡(132)+输入栏(70) 是固定开销，
# 降到 560 时只有 scale 0.70 能避开，模型会骤降一档。
FRAMING_SCALE = 0.80
FRAMING_OFFSET = (0.0, 0.15)

# 布局尺寸（改这里要同步 tools/measure_framing.py 里的同名常量）
BUBBLE_MARGIN = 16      # 气泡左右留白
BUBBLE_TOP = 46         # 气泡距窗口顶部：必须让开上面那排角标按钮
BUBBLE_MAX_H = 132      # 气泡最大高度；模型按这个上沿来避让
BUTTON_MARGIN = 10      # 角标按钮距窗口边缘
BUTTON_SIZE = 30        # 角标按钮边长（与 _corner_button 里的 setFixedSize 一致）

# 摄像头预览（视频对话开启时显示）。放左下角：模型是居中 128px 宽，
# 这块区域与模型、角标按钮、输入栏都不重叠。
PREVIEW_W = 96
PREVIEW_H = 72
PREVIEW_MARGIN = 12
PREVIEW_BOTTOM_GAP = 6  # 与输入栏顶边的间距

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
        self._tts_worker: Optional[TtsPipelineWorker] = None
        self._stt_worker: Optional[TranscribeWorker] = None
        self._recording = False
        # 初音模型顶部的实测位置（逻辑像素）；用来把角标按钮贴到她头顶上方
        self._model_top: Optional[int] = None

        # 远程服务（手机端）就绪后的地址，供气泡提示与设置面板显示
        self._remote_urls: list = []
        self._remote_info: dict = {}

        # 视频对话（视觉）
        self._video_enabled = SettingsDialog.video_enabled()
        # 已经配好、等着跟下一条消息一起发出去的画面（JPEG 字节，仅内存）
        self._pending_image: Optional[bytes] = None
        self._pending_source = "camera"
        self._camera: Optional[CameraSession] = None
        self._camera_worker: Optional[CameraWorker] = None

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
        self.input_bar.video_toggled.connect(self.set_video_enabled)
        self.input_bar.screen_requested.connect(self.capture_screen_for_reply)
        self.input_bar.hide()  # 初始隐藏，悬停才出现

        # 视频对话的实时预览。位置放在左下角：模型包围盒是居中 128px 宽，
        # 这块区域与模型、角标按钮、输入栏都不重叠。
        self.preview = QLabel(self)
        self.preview.setStyleSheet(
            "border: 2px solid rgba(57, 197, 187, 0.9); border-radius: 8px;"
            "background: #000;"
        )
        self.preview.setScaledContents(True)
        self.preview.setToolTip("摄像头预览（仅在视频对话开启时显示）")
        self.preview.hide()

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
        self.settings_dialog.video_toggled.connect(self.set_video_enabled)
        self.settings_dialog.reconfigure_requested.connect(self.open_reconfigure)
        self.tray = Tray(self, self.open_settings, self.quit_app)
        self.tray.show()

        self.model_clicked.connect(self._on_model_clicked)
        self.model_load_failed.connect(self._on_model_load_failed)
        # 气泡出现/消失时重排角标按钮：有气泡就回到顶端，没气泡就贴到初音头顶
        self.bubble.visibility_changed.connect(lambda _visible: self._layout_children())

        # 摄像头可用时才显示 📹；没有摄像头就退化成只有 🖥️ 截屏（零依赖路径）
        self.input_bar.set_video_visible(camera_available())
        self.input_bar.set_video_enabled(self._video_enabled)

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
        # 尺寸变了模型位置也变，延后重新量一次（避开连续 resize 抖动）
        if self.model is not None:
            QTimer.singleShot(250, self._measure_model_top)

    def _button_row_y(self) -> int:
        """角标按钮的纵向位置。

        - 有气泡时：待在窗口顶端（气泡从 BUBBLE_TOP 开始，正好让开）
        - 没气泡时：下移贴到初音头顶上方，避免顶部留一大片空白
        模型顶部是运行时实测的（Live2DView.measure_model_top），不是写死的常数。
        """
        if self.bubble.isVisible():
            return BUTTON_MARGIN
        if self._model_top is None:
            return BUTTON_MARGIN
        return max(BUTTON_MARGIN, self._model_top - BUTTON_SIZE - 10)

    def _layout_children(self) -> None:
        w, h = self.width(), self.height()
        btn_y = self._button_row_y()
        self.btn_min.move(BUTTON_MARGIN, btn_y)
        self.btn_close.move(BUTTON_MARGIN + BUTTON_SIZE + 6, btn_y)
        self.btn_settings.move(w - BUTTON_MARGIN - BUTTON_SIZE, btn_y)

        # 气泡：始终占满可用宽度（而不是随文字长短忽宽忽窄），
        # 这样短句也够大、长句换行整齐，不会再被头发挤成一小块。
        bar_h = 58
        bubble_w = max(160, w - BUBBLE_MARGIN * 2)
        self.bubble.setFixedWidth(bubble_w)
        self.bubble.setMaximumHeight(BUBBLE_MAX_H)
        self.bubble.adjustSize()
        self.bubble.move(max(0, (w - self.bubble.width()) // 2), BUBBLE_TOP)

        self.input_bar.setGeometry(12, h - bar_h - 12, w - 24, bar_h)

        # 摄像头预览贴左下角，位于输入栏上方
        self.preview.setGeometry(
            PREVIEW_MARGIN,
            h - bar_h - 12 - PREVIEW_BOTTOM_GAP - PREVIEW_H,
            PREVIEW_W,
            PREVIEW_H,
        )

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
        # 预览只在交互时露出：悬停或正在说话（视频通话的体感）
        show_preview = self._video_enabled and (self._hover or self._recording)
        self.preview.setVisible(show_preview)
        if show_preview:
            self.preview.raise_()

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

        # 视频模式下打字发送也配一帧，与语音路径保持一致体感。
        # 语音路径已经在 stop_voice_input() 取好帧了，这里不会重复取。
        if self._video_enabled and self._pending_image is None:
            self._take_vision_frame()
        image = self._consume_pending_image()

        self._busy = True
        self.input_bar.set_busy(True)
        self.input_bar.input.clear()
        self.bubble.show_typing()
        self._update_chrome()

        self._chat_worker = ChatWorker(
            self.agent, self.session_id, text, image, self
        )
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

    # ---------------------------------------------------------------- 远程服务
    def on_remote_ready(self, urls: list, info: dict) -> None:
        """远程服务就绪。

        由 main.py 用 Qt signal 从服务线程排队到这里，所以这里是主线程。

        `start.bat` 用的是 ``pythonw.exe``（无控制台），
        `print` 出来的地址用户根本看不到 —— 必须显式告诉他，
        否则「手机端」这个功能等于藏起来了。
        """
        self._remote_urls = list(urls or [])
        self._remote_info = dict(info or {})
        if not self._remote_urls:
            return
        print(f"[UI] 手机端可用：{'  '.join(self._remote_urls)}")
        # 必须晚于开场问候的隐藏定时器（_greet 里 9 秒后 hide_bubble），
        # 否则刚弹出来就被它顶掉。
        QTimer.singleShot(12000, self._announce_remote)

    def _announce_remote(self) -> None:
        if not self._remote_urls:
            return
        url = self._remote_urls[0]
        self.bubble.show_message(
            f"主人～手机连同一个 WiFi，打开 {url} 就能找到我啦♪",
            "HAPPY",
            12000,
        )
        self._layout_children()

    # -------------------------------------------------------------- 视频对话
    def set_video_enabled(self, enabled: bool) -> None:
        """开关视频对话。

        开：打开摄像头并启动预览线程（摄像头保持打开，抓帧才够快）。
        关：停线程 + 释放设备，LED 随之熄灭。
        """
        enabled = bool(enabled)
        if enabled:
            if self._camera_worker is None and not self._open_camera():
                return       # 打开失败时 _open_camera 内部已回滚
            self._video_enabled = True
        else:
            if self._camera_worker is not None:
                self._stop_camera()
            self._video_enabled = False
            # 摄像头来源的待发帧作废；截屏来源的保留（那是用户主动截的）
            if self._pending_source == "camera":
                self._pending_image = None
                self.input_bar.set_attached(False)

        self.input_bar.set_video_enabled(self._video_enabled)
        self._layout_children()
        self._update_chrome()

    def _open_camera(self) -> bool:
        if self._camera is None:
            self._camera = CameraSession()
        ok, reason = self._camera.open()
        if not ok:
            self._video_enabled = False
            self.input_bar.set_video_enabled(False)
            self.input_bar.set_video_visible(camera_available())
            self.bubble.show_message(
                f"呜…{reason}。要不要改用 🖥️ 截屏给我看？", "SAD", 6000
            )
            return False

        self._camera_worker = CameraWorker(self._camera, self)
        self._camera_worker.frame_ready.connect(self._on_preview_frame)
        self._camera_worker.failed.connect(self._on_camera_failed)
        self._camera_worker.start()
        print("[Vision] 摄像头已打开，视频对话开始")
        return True

    def _stop_camera(self) -> None:
        worker = self._camera_worker
        self._camera_worker = None
        if worker is not None:
            try:
                worker.stop()
                worker.wait(1500)
            except Exception:  # noqa: BLE001
                pass
        if self._camera is not None:
            self._camera.close()
        self.preview.clear()
        self.preview.hide()

    def _on_preview_frame(self, image) -> None:
        if self._video_enabled:
            self.preview.setPixmap(QPixmap.fromImage(image))

    def _on_camera_failed(self, message: str) -> None:
        """摄像头中途读不到画面（拔掉/被抢占）→ 自动降级回纯文本。"""
        print(f"[Vision] {message}")
        self._stop_camera()
        self._video_enabled = False
        self.input_bar.set_video_enabled(False)
        self.bubble.show_message(f"呜…{message}", "SAD", 6000)

    def capture_screen_for_reply(self) -> None:
        """截屏配到下一条消息上。

        截屏与剪贴板走 Qt 的 GUI 线程亲和接口（QScreen/QClipboard），
        耗时只有几十毫秒，所以直接在主线程做，不另开线程。
        """
        data = capture_screen() or capture_clipboard()
        if not data:
            self.bubble.show_message("诶…截屏失败了，换个方式给我看嘛？", "SAD", 4000)
            return
        self._pending_image = data
        self._pending_source = "screen"
        self.input_bar.set_attached(True, "screen")
        self.bubble.show_message("好哦，截图收到啦～想让我看什么？", "HAPPY", 4000)

    def _take_vision_frame(self) -> None:
        """在「说完那一刻」取一帧。

        直接取预览线程缓存的最新帧（最多落后 1/VISION_PREVIEW_FPS 秒），
        不去抢摄像头设备，所以**不产生任何采集延迟**；拿不到就降级为纯文本。
        """
        if not self._video_enabled or self._camera_worker is None:
            return
        data = self._camera_worker.take_latest()
        if data:
            self._pending_image = data
            self._pending_source = "camera"
            self.input_bar.set_attached(True, "camera")

    def _consume_pending_image(self) -> Optional[bytes]:
        data = self._pending_image
        self._pending_image = None
        self.input_bar.set_attached(False)
        return data

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
        # 「说完那一刻」取一帧给 Miku 看。
        # 读的是预览线程的缓存帧，和下面的 Whisper 转写并行，零额外延迟。
        self._take_vision_frame()
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
            return
        # 没识别出内容：别把刚才那张图留在「待发送」状态，否则下次打字会带上一张过期的画面
        self._consume_pending_image()
        if result.get("error"):
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
    def _settings_state(self) -> tuple:
        """收集设置面板要显示的状态。"""
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
        return health, stt_status, tts_status, nickname

    def open_settings(self) -> None:
        self.settings_dialog.load_state(*self._settings_state())
        self.settings_dialog.show()
        self.settings_dialog.raise_()
        self.settings_dialog.activateWindow()

    @staticmethod
    def reload_config() -> None:
        """重新读 .env 并 reload config 模块。

        config 的属性是模块导入时算好的常量，向导改的是 .env 文件，
        所以必须 load_dotenv(override=True) + reload 才能看到新值。
        """
        import importlib

        from dotenv import load_dotenv

        load_dotenv(config.BASE_DIR / ".env", override=True)
        importlib.reload(config)

    def open_reconfigure(self) -> None:
        """打开首次设置向导的「编辑模式」，改完**立即生效**，无需重启。

        这是本方法存在的理由 —— 光把值写进 .env 是不够的：
          * TTS/STT 的 engine / transcriber 在 ``__init__`` 就固化成实例字段
          * 本地 GPT-SoVITS 是**独立进程**且占约 2.2GB 显存，
            切到云端时不停掉它，显存会一直占着
          * Whisper 占约 1GB 内存，切到云端要卸载
          * REMOTE_ENABLED 以前只在启动时读一次

        所以这里：写 .env → reload config → 就地 reconfigure 各组件。
        就地改而不是重建对象，是因为 RemoteServer 等也持有同一批引用。
        """
        from PySide6.QtWidgets import QDialog

        from ui.setup_wizard import SetupWizard

        dialog = SetupWizard(self, edit_mode=True)
        # 称呼存在记忆库里、不在 .env，所以要单独回填
        try:
            dialog.nickname.setText(self.memory.get_meta("user_name") or "")
        except Exception:  # noqa: BLE001
            pass

        if dialog.exec() != QDialog.DialogCode.Accepted:
            return

        changed = dialog.apply()
        print(f"[Setup] 设置已更新：{changed}")
        self.reload_config()

        notes: list[str] = []

        # ---- 1) TTS 引擎（可能触发拉起/停掉 GPT-SoVITS 进程）----
        try:
            info = self.tts.reconfigure() if self.tts else {}
            if info.get("changed"):
                notes.append(f"语音输出 {info['previous']} → {info['engine']}")
                if info["engine"] == "sovits":
                    notes.append("正在后台加载 GPT-SoVITS（首次约 15 秒）")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"语音输出切换失败：{exc}")

        # ---- 2) STT 通道（可能卸载/加载 Whisper）----
        try:
            info = self.stt.reconfigure() if self.stt else {}
            if info.get("changed"):
                notes.append(f"语音输入 {info['previous']} → {info['transcriber']}")
                if info["transcriber"] == "local-whisper":
                    notes.append("正在后台加载 Whisper")
        except Exception as exc:  # noqa: BLE001
            notes.append(f"语音输入切换失败：{exc}")

        # ---- 3) 称呼 ----
        name = dialog.nickname.text().strip()
        if name:
            try:
                self.memory.set_meta("user_name", name)
                notes.append(f"称呼：{name}")
            except Exception as exc:  # noqa: BLE001
                print(f"[Setup] 保存称呼失败：{exc}")

        # ---- 4) 视频对话（向导写 .env，设置面板读 QSettings，这里对齐两者）----
        video_on = dialog.video.isChecked()
        self.settings_dialog.settings.setValue("video_enabled", video_on)
        if video_on != self._video_enabled:
            self.set_video_enabled(video_on)
            notes.append("视频对话：" + ("开" if video_on else "关"))

        # ---- 5) 远程服务启停 ----
        controller = getattr(self, "remote_controller", None)
        if controller is not None:
            try:
                action = controller.sync()
                if action == "started":
                    notes.append("手机端：已开启")
                elif action == "stopped":
                    notes.append("手机端：已关闭")
                elif action == "restarted":
                    notes.append("手机端：已用新端口重启")
                elif action == "failed":
                    notes.append("手机端启动失败，见 data/remote.log")
            except Exception as exc:  # noqa: BLE001
                notes.append(f"手机端切换失败：{exc}")

        # ---- 6) 刷新面板 + 气泡反馈 ----
        self.settings_dialog.load_state(*self._settings_state())
        message = "设置已更新♪" + ("\n" + "· ".join(notes) if notes else "")
        self.bubble.show_message(message, "HAPPY", 6000)

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
        # 等首帧画完再量模型顶部（GL 上下文可用之后才有意义）
        QTimer.singleShot(1300, self._measure_model_top)
        # 上次退出时视频对话是开着的 → 恢复它（延后一点，先让窗口画出来）
        if self._video_enabled:
            QTimer.singleShot(1800, lambda: self.set_video_enabled(True))

    def _measure_model_top(self) -> None:
        """实测模型顶部，用于把角标按钮贴到初音头顶上方。"""
        try:
            top = self.measure_model_top()
        except Exception as exc:  # noqa: BLE001
            print(f"[UI] 测量模型顶部失败：{exc}")
            return
        if top:
            self._model_top = top
            self._layout_children()

    def _greet(self) -> None:
        self.set_emotion("HAPPY")
        # 用 autohide_ms 而不是另外排一个 singleShot(hide_bubble)：
        # show_message 里的自动隐藏定时器会被新消息取消，而外部的
        # singleShot 不会 —— 那样用户如果在开场 9 秒内说话，
        # 回复刚显示出来就会被开场白的定时器隐藏掉。
        self.bubble.show_message(GREETING, "HAPPY", autohide_ms=9000)
        self._layout_children()

    def quit_app(self) -> None:
        self.stop_speaking()
        self._save_geometry()
        self.tray.hide()
        self.close()
        QApplication.quit()

    def closeEvent(self, event) -> None:  # noqa: N802
        self.stop_speaking()
        self._save_geometry()
        self._stop_camera()          # 先放掉摄像头，摄像头 LED 随之熄灭
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
