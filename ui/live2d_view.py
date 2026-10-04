"""Live2D 渲染视图（live2d-py / Cubism Native，无 Web Engine）。

职责：
  - 在透明 OpenGL 窗口里渲染 Miku
  - 按**模型画像（profile）**把情感标签映射到动作组 / 表情 / 头部倾角
  - 视线跟随、点击互动、拖拽移动窗口
  - 口型同步：优先用 WavHandler 读真实音频包络，无音频时退回正弦模拟
  - **运行时换模型**（两套模型都保留，桌面端可切）
  - **自动取景**：按实测的美术包围盒把模型拟合进可用区域

关于换模型（重要，踩过）：
两个模型不能同时活着。旧模型必须显式 `DestroyRenderer()` 之后才能建新的 ——
否则旧渲染器残留的 GL 资源会让**新模型画不出来**（实测：先加载经典模型、
再把它的 PyModelObject 交给 GC，然后加载新模型 → 新模型 alpha 全 0，几乎空白）。
所以 `load_model()` 里是「销毁渲染器 → 丢引用 → 建新的」这个固定顺序。

关于取景（也踩过）：
新模型的**美术范围超出了画布**（画布 3500x8888，美术 x 782~5158、y -91~8898），
而且原生渲染器是按画布投影的，所以 scale=1 时它大半个身子都在窗口外。
写死一套 scale/offset 只对一个模型成立，换个模型就废 —— 改成**实测拟合**：
把模型缩到能完整看见，量出美术的真实屏幕尺寸，算出目标 scale，
再实测「1 个 offset 单位等于多少像素」，据此居中。
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
from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QCursor, QGuiApplication
from PySide6.QtOpenGLWidgets import QOpenGLWidget

import models_catalog

# 点击时随机播放的动作池（对齐旧版 live2d.js）。模型没有这些组时会自动回退到
# 它自己的 Idle 组，所以列表里多写几个名字是安全的。
CLICK_MOTIONS = ["Tap", "Tap", "Flick", "FlickUp", "Dance", "Idle"]

# 自动取景的粗扫 scale：先缩到这么小，保证整块美术都在窗口里，才量得准。
# 取 0.25 是因为最大的模型（moc3 v5）在 0.25 时美术约 81x168 逻辑像素，绰绰有余。
_FIT_SCAN_SCALE = 0.25
# 量 offset 单位的试探步长；太大可能把模型推出窗口（读数被裁切后就不准了）
_FIT_OFFSET_STEP = 0.1


class Live2DView(QOpenGLWidget):
    """透明置顶的 Live2D 画布，同时充当桌宠主窗口。"""

    model_clicked = Signal()
    model_load_failed = Signal(str)
    # 模型加载完成后带出画像信息（画布/参数/表情数），供控制台显示
    model_loaded = Signal(dict)

    def __init__(
        self,
        model_id: str,
        fps: int = 60,
        lipsync_gain: float = 1.6,
        framing_scale: float = 1.0,
        framing_offset: tuple[float, float] = (0.0, 0.0),
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.model_id = model_id
        self.model_path = models_catalog.model_json_path(model_id)
        self.profile: dict = dict(models_catalog.resolve(model_id).get("profile") or {})

        self.fps = max(10, min(144, fps))
        self.lipsync_gain = lipsync_gain
        # 自动取景完成前用的初值；取景成功后会被实测值覆盖
        self.framing_scale = framing_scale
        self.framing_offset = framing_offset

        self.model: Optional[live2d.LAppModel] = None
        self.expression_ids: list[str] = []
        self.motion_groups: dict = {}
        self.param_ids: list[str] = []
        self.param_index: dict[str, int] = {}
        self.model_info: dict = {}

        # 水印：True = 显示。默认不显示（用户要求去掉水印）。
        self.watermark_visible = False
        self._wm_param: Optional[str] = self.profile.get("watermark_param")

        # 口型
        self._wav: Optional[WavHandler] = None
        self._speak_fallback = False
        self._speak_start = 0.0

        # 头部倾角（情绪表达）。倾角表为 0 的模型（经典模型）相当于关掉。
        self._tilt_target = 0.0
        self._tilt_now = 0.0

        # 待机动画
        self.idle_enabled = True
        self.idle_group = "Idle"
        self.idle_gap_range = (2.5, 7.0)
        self._next_idle_at = 0.0
        self._breath_t0 = time.time()
        # 换模型后重置：新模型的 MotionManager 还没被启动过，见 _update_idle 的说明
        self._played_any = False

        # 交互
        self._pressed_in_model = False
        self._drag_origin = None
        self._win_origin = None

        # 自动取景状态
        self._fit_rect: Optional[QRect] = None
        self._calibrating = False
        # 取景用的离屏 FBO（绝不用控件自己的 FBO 画中间帧，见 _probe_box）
        self._probe_fbo = 0
        self._probe_tex = 0
        self._probe_size = (0, 0)
        # 最近一次实测的美术包围盒 [left, top, right, bottom]（逻辑像素）。
        # 外层用它把角标按钮贴到初音头顶上方。
        self.model_box: Optional[list[float]] = None

        # 透明 + 无边框 + 置顶 + 不进任务栏
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        # 注意：这里**不能**设 WA_AlwaysStackOnTop。
        # 它会让 GL 内容无视正常层叠顺序、永远画在最上面，
        # 结果气泡 / 角标按钮这些子控件反而被模型盖住（已实测确认）。
        self.setMouseTracking(True)
        self.setWindowTitle("MikuAgent")
        self.setCursor(Qt.CursorShape.ArrowCursor)

    # ------------------------------------------------------------------ OpenGL
    def initializeGL(self) -> None:
        live2d.glInit()
        self._load_model()

    def _load_model(self) -> bool:
        """加载 `self.model_path` 指向的模型。成功返回 True。

        调用前请确保旧模型已经被 `_unload_model()` 收掉。
        """
        self.expression_ids = []
        self.motion_groups = {}
        self.param_ids = []
        self.param_index = {}

        # 先自己检查文件是否存在：Cubism 的 LoadModelJson 在文件缺失时
        # 不是抛异常而是**直接卡死**，必须提前拦住。
        if not self.model_path.exists():
            self.model = None
            self.model_load_failed.emit(f"模型文件不存在：{self.model_path}")
            return False

        try:
            self.model = live2d.LAppModel()
            self.model.LoadModelJson(str(self.model_path))
            self.model.SetAutoBlinkEnable(True)
            self.model.SetAutoBreathEnable(True)
            # ⚠️ 必须**立刻** Resize 一次，不能等 resizeGL。
            #
            # Resize 设的是这个模型自己的投影矩阵。首次加载时 Qt 会在
            # initializeGL 之后调一次 resizeGL，所以看起来「不用管」；
            # 但**换模型时不会再有 resizeGL** —— 新模型就一直停在默认投影上，
            # 表现为画出来只有右上角一小块，而且自动取景会拿这个错误的投影
            # 去反推，算出偏小/比例不对的 scale（用户报的「换模型后模型变瘦了」
            # 就是这个）。实测：直接加载 160x345，换过之后 82x121。
            self.model.Resize(int(self.width() * self._dpr),
                              int(self.height() * self._dpr))
            self._apply_framing()
        except Exception as exc:  # noqa: BLE001
            # 模型/驱动有问题时不要让整个程序崩掉：置空模型并通知上层回退到静态立绘
            print(f"[Live2D] 模型加载失败：{exc}")
            self.model = None
            self.model_load_failed.emit(str(exc))
            return False

        self._discover_assets()

        # 参数表：不同模型命名差异很大（经典模型用大写 PARAM_*，新模型用
        # Cubism 标准名 ParamAngleZ），很多「标准名」写法在某个模型上会静默失效，
        # 所以要先拿到真实 ID 列表 + 索引，之后按名字查。
        try:
            self.param_ids = list(self.model.GetParamIds())
            self.param_index = {name: i for i, name in enumerate(self.param_ids)}
        except Exception:  # noqa: BLE001
            self.param_ids = []
            self.param_index = {}

        try:
            canvas = self.model.GetCanvasSize()
        except Exception:  # noqa: BLE001
            canvas = (0.0, 0.0)

        self.model_info = {
            "id": self.model_id,
            "name": models_catalog.resolve(self.model_id)["name"],
            "path": str(self.model_path),
            "canvas": [round(float(canvas[0]), 4), round(float(canvas[1]), 4)],
            "drawables": len(self.model.GetDrawableIds()),
            "params": len(self.param_ids),
            "expressions": list(self.expression_ids),
            "motion_groups": dict(self.motion_groups),
            "watermark_param": self._wm_param,
        }
        self.startTimer(int(1000 / self.fps))
        self.update()
        print(f"[Live2D] 已加载 {self.model_id}："
              f"{self.model_info['drawables']} drawable / {self.model_info['params']} 参数 / "
              f"{len(self.expression_ids)} 表情 / 动作组 {list(self.motion_groups)}")
        self.model_loaded.emit(dict(self.model_info))
        return True

    def _discover_assets(self) -> None:
        """收集表情与动作组；model3.json 里没写就从同目录的散装文件补装。

        为什么需要：新模型（moc3 v5）的 model3.json 里 `Motions` 与 `Expressions`
        **都是空的** —— 8 个表情是独立的 `.exp3` 文件，VTube Studio 靠自己的
        `miku.vtube.json` 热键表去加载它们。标准 Cubism 运行时不会自动发现这些
        文件，于是 `SetExpression("比心")` 找不到东西，点了也没反应。

        模型授权写明「不可二传二改」，所以**不能改盘上的 model3.json**；
        这里用 LoadExtraExpression / LoadExtraMotion 只改内存里的注册表。

        ⚠️ 补装的东西**不会出现在 GetExpressionIds() / GetMotionGroups() 里**
        （实测：补装成功后那两个接口依然返回空列表，但 SetExpression / StartMotion
        是生效的 —— `SetExpression("比心")` 实测改动了 5 个参数）。所以必须自己把
        名字记下来，否则「表情列表为空 → 不设表情」「动作组为空 → 永远不播待机」，
        表现就是模型一脸呆滞地站着。
        """
        if self.model is None:
            return
        try:
            self.expression_ids = list(self.model.GetExpressionIds())
        except Exception:  # noqa: BLE001
            self.expression_ids = []
        try:
            self.motion_groups = dict(self.model.GetMotionGroups())
        except Exception:  # noqa: BLE001
            self.motion_groups = {}

        if not self.profile.get("auto_scan_assets"):
            return

        folder = self.model_path.parent
        if not self.expression_ids:
            loaded: list[str] = []
            for path in sorted(folder.glob("*.exp3.json"), key=lambda p: p.name):
                name = path.name[: -len(".exp3.json")]
                try:
                    self.model.LoadExtraExpression(name, str(path))
                    loaded.append(name)
                except Exception as exc:  # noqa: BLE001
                    print(f"[Live2D] 补装表情 {name} 失败：{exc}")
            # 自记的名字 ∪ 接口报出来的（不同版本行为不一致，取并集最稳）
            try:
                loaded += list(self.model.GetExpressionIds())
            except Exception:  # noqa: BLE001
                pass
            self.expression_ids = sorted(set(loaded))
            print(f"[Live2D] 补装 {len(self.expression_ids)} 张表情：{self.expression_ids}")

        if not self.motion_groups:
            added = 0
            for path in sorted(folder.glob("*.motion3.json"), key=lambda p: p.name):
                try:
                    self.model.LoadExtraMotion(self.idle_group, str(path))
                    added += 1
                except Exception as exc:  # noqa: BLE001
                    print(f"[Live2D] 补装动作 {path.name} 失败：{exc}")
            # LAppModel.GetMotions() 会把结果缓存在 _motions_cache 里，而补装的
            # 动作压根不会进这个缓存 —— 先清掉再取，取不到就自己记数。
            try:
                self.model._motions_cache = None
            except Exception:  # noqa: BLE001
                pass
            try:
                self.motion_groups = dict(self.model.GetMotionGroups())
            except Exception:  # noqa: BLE001
                self.motion_groups = {}
            if added and self.idle_group not in self.motion_groups:
                self.motion_groups[self.idle_group] = added
            print(f"[Live2D] 补装 {added} 个动作 → 动作组 {self.motion_groups}")

        # 补装的动作全挂在 idle_group 下；若该组仍为空，退而用它实际拥有的第一个组，
        # 否则待机调度会一直找不到可播的动作（模型就会僵着不动）。
        if self.idle_group not in self.motion_groups and self.motion_groups:
            fallback = next(iter(self.motion_groups))
            print(f"[Live2D] 没有 Idle 组，待机改用 {fallback}")
            self.idle_group = fallback

    def _unload_model(self) -> None:
        """彻底释放当前模型。

        顺序很重要：**先 DestroyRenderer**（它还持有 GL 纹理，必须在 GL 上下文
        还活着的时候销毁），再丢掉 Python 引用并强制 GC。反过来做的话，
        C++ 对象的析构时机不可控，很容易在下一次换模型时表现为「新模型画不出来」。
        """
        model = self.model
        self.model = None
        if model is not None:
            try:
                model.DestroyRenderer()
            except Exception as exc:  # noqa: BLE001
                print(f"[Live2D] 销毁渲染器失败（继续换模型）：{exc}")
            del model
        import gc

        gc.collect()

    def load_model(self, model_id: str) -> bool:
        """运行时换模型。返回是否成功。

        失败时不会留下「半个模型」：先把旧的收干净，再装新的；
        新的装不上就置空并让上层回退到静态立绘。

        ⚠️ 整个过程必须**持有当前 GL 上下文**（makeCurrent）。

        首次加载发生在 `initializeGL` 里，那时上下文天然是当前的；而换模型是从
        按钮回调里进来的，上下文不是当前的 —— `LoadModelJson` / `CreateRenderer`
        / `DestroyRenderer` 都会真的发 GL 调用，脱离上下文建出来的渲染器是坏的，
        画出来只有角落一小块，而且自动取景会拿这份坏投影去反推，算出偏小的 scale
        （用户报的「换模型之后模型变瘦/跑到角落」就是这个）。
        实测：直接加载 160x345，换过之后 82x121。
        """
        if model_id == self.model_id and self.model is not None:
            return True
        print(f"[Live2D] 切换模型：{self.model_id} → {model_id}")
        self.stop_lipsync()

        self.makeCurrent()
        try:
            self._unload_model()

            self.model_id = model_id
            self.model_path = models_catalog.model_json_path(model_id)
            self.profile = dict(models_catalog.resolve(model_id).get("profile") or {})
            self._wm_param = self.profile.get("watermark_param")
            self.idle_group = "Idle"
            self._tilt_target = 0.0
            self._tilt_now = 0.0
            self._played_any = False
            # 待机计时也要重置：不然换过来之后要等上一个模型留下的间隔
            # （最多 7 秒）才会动，看着像「换模型后僵住了」。
            self._next_idle_at = 0.0
            self.framing_scale = 1.0
            self.framing_offset = (0.0, 0.0)
            self._fit_rect = None

            ok = self._load_model()
        finally:
            self.doneCurrent()

        if ok and self._fit_rect is not None:
            # 换模型后重新拟合：每个模型的美术范围完全不同
            self.auto_frame(self._fit_rect)
        return ok

    def resizeGL(self, w: int, h: int) -> None:
        if self.model is not None:
            self.model.Resize(w, h)
            self._apply_framing()

    def _apply_framing(self) -> None:
        """整体缩放与偏移。"""
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

    def _set_transform(self, scale: float, offset: tuple[float, float]) -> None:
        """只改变换，不触发重绘 —— 取景过程中要连续调很多次，没必要每次都排一次 paint。"""
        self.framing_scale = scale
        self.framing_offset = offset
        self._apply_framing()

    def paintGL(self) -> None:
        live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)  # 全透明
        if self.model is None:
            return
        self.model.Update()
        self.model.Draw()

    # --------------------------------------------------------------- 自动取景
    @property
    def _dpr(self) -> float:
        try:
            return float(self.devicePixelRatioF()) or 1.0
        except Exception:  # noqa: BLE001
            return float(QGuiApplication.primaryScreen().devicePixelRatio()) or 1.0

    def _probe_box(self) -> Optional[list[float]]:
        """自己画一帧并读回 alpha 包围盒（逻辑像素，原点左上）。

        为什么不直接画进控件自己的 framebuffer：取景要连画十几帧，而这些绘制
        发生在 paintGL **之外**。控件的 FBO 是会被合成到屏幕上的 —— 在里面留下
        的中间帧会被当成画面显示出来，用户看到的就是「模型重影/两个分身」
        （实测截图确认：屏幕上两个错位的 Miku，而 grabFramebuffer 是干净的）。
        所以这里自己建一个离屏 FBO，量完就还回控件的默认 FBO。
        """
        if self.model is None:
            return None
        w = int(self.width() * self._dpr)
        h = int(self.height() * self._dpr)
        if w <= 0 or h <= 0:
            return None
        self.makeCurrent()
        try:
            fbo, tex = self._ensure_probe_fbo(w, h)
            if fbo == 0:
                return None
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
            gl.glViewport(0, 0, w, h)
            live2d.clearBuffer(0.0, 0.0, 0.0, 0.0)
            self.model.Update()
            self.model.Draw()
            raw = gl.glReadPixels(0, 0, w, h, gl.GL_RGBA, gl.GL_UNSIGNED_BYTE)
            # 立刻还回控件自己的 FBO，别把离屏内容留给后续的 paintGL
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        except Exception:  # noqa: BLE001
            return None
        finally:
            self.doneCurrent()
        if not raw:
            return None
        arr = np.frombuffer(raw, dtype=np.uint8)
        if arr.size < w * h * 4:
            return None
        alpha = arr.reshape(h, w, 4)[:, :, 3]
        rows = np.where(alpha.max(axis=1) > 8)[0]
        cols = np.where(alpha.max(axis=0) > 8)[0]
        if len(rows) == 0 or len(cols) == 0:
            return None
        dpr = self._dpr
        # GL 原点在左下：行号越大越靠上，所以要翻过来
        return [
            int(cols.min()) / dpr,
            (h - 1 - int(rows.max())) / dpr,
            int(cols.max()) / dpr,
            (h - 1 - int(rows.min())) / dpr,
        ]

    def _ensure_probe_fbo(self, w: int, h: int) -> tuple[int, int]:
        """按需创建/复用离屏 FBO（尺寸变了就重建）。返回 (fbo, texture)，失败为 (0, 0)。"""
        if self._probe_fbo and self._probe_size == (w, h):
            return self._probe_fbo, self._probe_tex
        self._release_probe_fbo()
        tex = int(gl.glGenTextures(1))
        gl.glBindTexture(gl.GL_TEXTURE_2D, tex)
        gl.glTexImage2D(gl.GL_TEXTURE_2D, 0, gl.GL_RGBA8, w, h, 0,
                        gl.GL_RGBA, gl.GL_UNSIGNED_BYTE, None)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MIN_FILTER, gl.GL_LINEAR)
        gl.glTexParameteri(gl.GL_TEXTURE_2D, gl.GL_TEXTURE_MAG_FILTER, gl.GL_LINEAR)
        fbo = int(gl.glGenFramebuffers(1))
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, fbo)
        gl.glFramebufferTexture2D(gl.GL_FRAMEBUFFER, gl.GL_COLOR_ATTACHMENT0,
                                  gl.GL_TEXTURE_2D, tex, 0)
        status = gl.glCheckFramebufferStatus(gl.GL_FRAMEBUFFER)
        if status != gl.GL_FRAMEBUFFER_COMPLETE:
            print(f"[Live2D] 离屏 FBO 不完整（status={status}），取景退回控件 FBO")
            gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.defaultFramebufferObject())
            gl.glDeleteFramebuffers(1, [fbo])
            gl.glDeleteTextures([tex])
            return 0, 0
        gl.glBindFramebuffer(gl.GL_FRAMEBUFFER, self.defaultFramebufferObject())
        self._probe_fbo, self._probe_tex, self._probe_size = fbo, tex, (w, h)
        return fbo, tex

    def _release_probe_fbo(self) -> None:
        if self._probe_fbo:
            try:
                gl.glDeleteFramebuffers(1, [self._probe_fbo])
                gl.glDeleteTextures([self._probe_tex])
            except Exception:  # noqa: BLE001
                pass
        self._probe_fbo = 0
        self._probe_tex = 0
        self._probe_size = (0, 0)

    @staticmethod
    def _box_w(box) -> float:
        return box[2] - box[0]

    @staticmethod
    def _box_h(box) -> float:
        return box[3] - box[1]

    @staticmethod
    def _box_cx(box) -> float:
        return (box[0] + box[2]) / 2.0

    @staticmethod
    def _box_cy(box) -> float:
        return (box[1] + box[3]) / 2.0

    def _box_touches_edge(self, box, slack: float = 1.5) -> bool:
        """包围盒是否贴住了窗口边缘 —— 贴住就说明被裁了，读数不能用来推算。"""
        return (
            box[0] <= slack or box[1] <= slack
            or box[2] >= self.width() - slack or box[3] >= self.height() - slack
        )

    def auto_frame(self, avail: QRect) -> bool:
        """按实测把美术包围盒拟合进 `avail`（窗口逻辑坐标）。

        为什么要实测而不是查表：两个模型的美术范围差一个数量级，而且美术**超出
        画布**（新模型画布 3500x8888，美术 x 782~5158）。写死的 scale/offset
        只对一个模型成立；实测则天然适配任何模型、任何窗口尺寸。
        """
        if self.model is None or avail.width() < 40 or avail.height() < 40:
            return False
        self._fit_rect = QRect(avail)

        # 校准期间必须让模型**别自己动**：眨眼、呼吸、待机动作都会让包围盒
        # 在几次采样之间漂移，换算出来的 offset 就会带上误差。
        blink = breath = None
        self._calibrating = True
        try:
            try:
                self.model.SetAutoBlinkEnable(False)
                self.model.SetAutoBreathEnable(False)
                blink = breath = True
            except Exception:  # noqa: BLE001
                pass
            try:
                self.model.StopAllMotions()
            except Exception:  # noqa: BLE001
                pass

            # ---- 1) 粗扫：缩到能完整看见，量出美术在 scale=1 时的屏幕尺寸 ----
            scale = _FIT_SCAN_SCALE
            box = None
            for _ in range(5):
                self._set_transform(scale, (0.0, 0.0))
                box = self._probe_box()
                if box is not None and not self._box_touches_edge(box):
                    break
                scale *= 0.5
            if box is None:
                return False
            art_w = max(1.0, self._box_w(box)) / scale
            art_h = max(1.0, self._box_h(box)) / scale

            target_scale = min(avail.width() / art_w, avail.height() / art_h)
            target_scale = max(0.005, min(8.0, target_scale))

            # ---- 2) 居中：实测「1 个 offset 单位 = 多少像素」后换算 ----
            self._set_transform(target_scale, (0.0, 0.0))
            base = self._probe_box()
            if base is None:
                return False

            tx, ty = avail.center().x(), avail.center().y()
            ox = oy = 0.0
            step = _FIT_OFFSET_STEP
            for _ in range(3):
                # 单边试探：位移太大可能把模型推出窗口（读数被裁就作废），逐步减小
                kx = ky = None
                while step > 0.006:
                    self._set_transform(target_scale, (ox + step, oy))
                    bx = self._probe_box()
                    self._set_transform(target_scale, (ox, oy + step))
                    by = self._probe_box()
                    if bx is None or by is None:
                        step /= 2.0
                        continue
                    if self._box_touches_edge(bx) or self._box_touches_edge(by):
                        step /= 2.0
                        continue
                    kx = (self._box_cx(bx) - self._box_cx(base)) / step
                    # 注意符号：+dy 是**向上**，所以这里的斜率天然是负的
                    ky = (self._box_cy(by) - self._box_cy(base)) / step
                    break
                if kx is None or ky is None or abs(kx) < 1e-6 or abs(ky) < 1e-6:
                    break
                ox += (tx - self._box_cx(base)) / kx
                oy += (ty - self._box_cy(base)) / ky
                self._set_transform(target_scale, (ox, oy))
                base = self._probe_box()
                if base is None:
                    return False
                if abs(self._box_cx(base) - tx) <= 2.0 and abs(self._box_cy(base) - ty) <= 2.0:
                    break

            self._set_transform(target_scale, (ox, oy))
            final = self._probe_box()
            self.model_box = final or base
            got = (f"{self._box_w(final):.0f}x{self._box_h(final):.0f}"
                   if final else "末次测量失败")
            print(f"[Live2D] 自动取景 {self.model_id}: scale={target_scale:.4f} "
                  f"offset=({ox:.4f}, {oy:.4f}) 美术={art_w:.0f}x{art_h:.0f} "
                  f"可用={avail.width()}x{avail.height()} 实测={got}")
            return True
        finally:
            self._calibrating = False
            if blink:
                try:
                    self.model.SetAutoBlinkEnable(True)
                    self.model.SetAutoBreathEnable(True)
                except Exception:  # noqa: BLE001
                    pass
            self.update()

    # --------------------------------------------------------------- 帧循环
    def timerEvent(self, event) -> None:  # noqa: N802
        if not self.isVisible():
            return

        if self.model is not None:
            now = time.time()

            # 视线跟随鼠标（窗口内坐标）
            local = self.mapFromGlobal(QCursor.pos())
            try:
                self.model.Drag(local.x(), local.y())
            except Exception:  # noqa: BLE001
                pass

            # 待机动画：动作播完就接下一个，否则模型会一直僵在默认姿态
            if not self._calibrating:
                self._update_idle(now)
                self._update_breath(now)
                self._update_tilt()
                self._update_watermark()

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
        """播放动作组。组不存在时回退到待机组，保证「点了有反应」。"""
        if self.model is None:
            return
        if group not in self.motion_groups:
            # 经典模型有 Tap/Flick/Cry…，新模型只有 Idle —— 直接跳过会让
            # 交互看起来像坏了，所以退到待机组播一个。
            group = self.idle_group
        if group not in self.motion_groups:
            return
        try:
            self.model.StartRandomMotion(
                group=group,
                priority=priority if priority is not None else live2d.MotionPriority.NORMAL,
            )
            self._played_any = True
            # 非待机动作播完后，让待机调度等一会儿再来。
            # 不等的话，IsMotionFinished() 刚变 True 而原生那边优先级还没放开，
            # 就会刷一屏 "motion priority is too low."（原生只是警告，但很吵）。
            if priority is None or priority > live2d.MotionPriority.IDLE:
                self._next_idle_at = time.time() + 3.0
        except Exception:  # noqa: BLE001
            pass

    def _pick_idle_motion(self) -> Optional[int]:
        """在待机组里加权挑一个动作序号。

        实测经典模型 Idle 组有 3 个：07_点头(1.3s)、14_点头(2.9s)、09_渐入睡眠(21s)。
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
        """待机调度：当前动作播完后，隔一小段随机时间再接一个待机动作。

        这是「看着僵」的根因修复 —— 原来动作只在收到回复和点击时各播一次，
        播完就停在默认姿态。另外实测经典模型的 motion3.json 虽然写了 Loop=true，
        运行时 IsMotionFinished() 仍会在几秒后变 True，并不循环。

        ⚠️ 「忙不忙」这个判断必须带 `_played_any` 条件：**刚加载的模型**
        `IsMotionFinished()` 会返回 False（动作管理器还没被启动过），
        照它字面意思理解就是「永远在忙」，于是待机动作一个都播不出来，
        模型从头到尾僵着 —— 实测两个模型首次加载时都是这样。
        """
        if not self.idle_enabled or self.model is None or self.speaking:
            return
        if self._played_any:
            try:
                if not self.model.IsMotionFinished():
                    return          # 正忙着（回复动作 / 点击动作），别抢
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
                self._played_any = True
            except Exception:  # noqa: BLE001
                self.play_motion(self.idle_group, priority=live2d.MotionPriority.IDLE)
        lo, hi = self.idle_gap_range
        self._next_idle_at = now + random.uniform(lo, hi)

    def _update_breath(self, now: float) -> None:
        """手动驱动呼吸 —— 只对「自动呼吸失效」的模型开。

        经典模型的参数叫 PARAM_BREATH（大写），而 SetAutoBreathEnable() 只认标准名
        ParamBreath，所以自动呼吸在那个模型上**完全失效**，必须自己驱动。
        新模型用的就是标准名 ParamBreath，自动呼吸有效，再手动叠加只会打架，
        所以这里按模型画像决定要不要驱动。
        """
        if self.model is None or not self.profile.get("manual_breath"):
            return
        # PARAM_BREATH 取值 0~1；频率约 0.22Hz，接近真人静息呼吸
        phase = (math.sin((now - self._breath_t0) * 1.4) + 1.0) * 0.5
        self.set_param(phase, "PARAM_BREATH", "ParamBreath")

    def _update_tilt(self) -> None:
        """头部/身体微倾，用来表情绪（新模型的 ParamAngleZ 是标准名）。"""
        if self.model is None:
            return
        if self._tilt_now == 0.0 and self._tilt_target == 0.0:
            return
        self._tilt_now += (self._tilt_target - self._tilt_now) * 0.12
        self.set_param(self._tilt_now, "ParamAngleZ", "PARAM_ANGLE_Z")
        self.set_param(self._tilt_now * 0.35, "ParamBodyAngleZ", "PARAM_BODY_ANGLE_Z")

    def _update_watermark(self) -> None:
        """水印参数每帧直写。

        为什么不走「水印」表情：那个 exp3 只是把 Param137 加 1，而表情在切换情绪时
        会被整体 ResetExpression 重置，水印就会自己冒回来。直写参数最可靠。
        """
        if self.model is None or not self._wm_param:
            return
        hidden = float(self.profile.get("watermark_hidden_value", 1.0))
        shown = float(self.profile.get("watermark_shown_value", 0.0))
        self.set_param(shown if self.watermark_visible else hidden, self._wm_param)

    def set_watermark_visible(self, visible: bool) -> None:
        self.watermark_visible = bool(visible)
        self._update_watermark()

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

        不同模型命名不一致，直接写标准名（如 ParamBrowLY）在另一个模型上会静默失效
        —— 传 None 也不会报错，只是那行代码什么都不做，很难发现。
        """
        for name in candidates:
            if name in self.param_index:
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
        """情感标签 → 动作 + 表情 + 倾角（映射表来自模型画像）。"""
        emotion = (emotion or "NORMAL").upper()
        motions = self.profile.get("emotion_motion") or {}
        expressions = self.profile.get("emotion_expression") or {}
        tilts = self.profile.get("emotion_tilt") or {}

        self.play_motion(motions.get(emotion, self.idle_group))
        self.set_expression(expressions.get(emotion))
        try:
            self._tilt_target = float(tilts.get(emotion, 0.0) or 0.0)
        except Exception:  # noqa: BLE001
            self._tilt_target = 0.0

        # 生气时手动压低眉毛。经典模型用大写名，新模型用标准名，两边都试。
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
        dpr = self._dpr
        px = int(x * dpr)
        py = int((h - y) * dpr)
        if px < 0 or py < 0 or px >= int(w * dpr) or py >= int(h * dpr):
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
        box = self._probe_box()
        if box is None:
            return None
        return int(box[1])

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
        if self._pressed_in_model and self._drag_origin is not None:
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
        self.play_motion(random.choice(CLICK_MOTIONS))
        self.model_clicked.emit()

    # ------------------------------------------------------------------ 清理
    def shutdown(self) -> None:
        """收掉渲染。**必须在 GL 上下文当前时做**（DestroyRenderer 会发 GL 调用）。"""
        self.stop_lipsync()
        try:
            self.makeCurrent()
            try:
                self._release_probe_fbo()
                self._unload_model()
            finally:
                self.doneCurrent()
        except Exception:  # noqa: BLE001
            # 窗口可能已经销毁、上下文拿不到了；这时交给进程退出时回收
            self.model = None
