"""模型目录：桌面端与手机端**共用**的一份模型清单 + 每个模型的渲染画像（profile）。

为什么要单独一个模块：

以前「用哪个模型」是硬编码在两个地方的 —— 桌面端 `config.MODEL_PATH` 指向
旧模型，手机端靠 `config.REMOTE_MODEL_DIR` 指向新模型。两边各写各的，
于是「换模型」这件事根本无法表达，加第二个模型只能靠改代码。

现在把它变成**数据**：
  * `MODELS` 是清单，每个模型有 id / 名字 / 目录 / model3.json 文件名；
  * `profile` 是渲染画像 —— 情绪对应哪张表情、要不要手动驱动呼吸、
    水印参数叫什么、值为多少算「藏起来」。
  * 手机端通过 `/model/<id>/manifest` 把同一份 profile 一起取走，
    所以**两端的行为天然一致**，不会再出现「手机上会脸红、电脑上不会」。

「当前用哪个」记在 `data/model_prefs.json`（桌面一个、手机一个），
不放进 .env：它是运行期可切换的状态，不是部署配置；而且手机端模型是
在 PC 控制台上点的，写 .env 会让人误以为要重启。

模型文件本身永不入库（授权写明「不可二传二改」），这里只引用路径。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import config

PREFS_PATH = Path(config.DATA_DIR) / "model_prefs.json"

# 情绪标签全集（与 backend/agent.py 解析出来的标签一致）
EMOTIONS = ("HAPPY", "SAD", "ANGRY", "SURPRISED", "MOTIVATED", "EMPATHY", "NORMAL")


def _tilt(happy: int, sad: int, angry: int, surprised: int,
          motivated: int, empathy: int) -> dict:
    return {
        "HAPPY": happy, "SAD": sad, "ANGRY": angry, "SURPRISED": surprised,
        "MOTIVATED": motivated, "EMPATHY": empathy, "NORMAL": 0,
    }


MODELS: list[dict] = [
    {
        "id": "miku",
        "name": "初音ミク · 经典",
        "short": "经典",
        "note": "moc3 v4 · 1 张 4096² 贴图 · 13 个动作 / 6 张表情",
        "dir": config.ASSETS_DIR / "live2d" / "miku",
        "model3": "miku.model3.json",
        "profile": {
            # 情绪 → 动作组（对应 miku.model3.json 的 Motions）
            "emotion_motion": {
                "HAPPY": "Tap", "ANGRY": "Flick", "SURPRISED": "FlickUp",
                "SAD": "Cry", "MOTIVATED": "Tap", "EMPATHY": "Idle", "NORMAL": "Idle",
            },
            # 情绪 → 表情。注意 Dazhihui / Mimiyan 两张 exp3 是**坏素材**
            # （会画出错位红条和翻白眼，实测逐个应用确认过），所以弃用。
            "emotion_expression": {
                "HAPPY": "Saihong", "MOTIVATED": "Saihong", "SURPRISED": "Chijing",
                "EMPATHY": "liuhan", "SAD": "liuhan", "ANGRY": None, "NORMAL": None,
            },
            # 这个模型没有 ParamAngleZ（参数全是大写 PARAM_*），给 0 表示不走倾角，
            # 生气的表现交给 Flick 动作 + 手动压眉毛。
            "emotion_tilt": _tilt(0, 0, 0, 0, 0, 0),
            # 水印：该模型没有水印参数
            "watermark_param": None,
            # 呼吸必须手动驱动：模型参数叫 PARAM_BREATH（大写），
            # SetAutoBreathEnable 只认标准名 ParamBreath，自动呼吸在本模型上是失效的。
            "manual_breath": True,
            # 动作/表情写在 model3.json 里了，不需要扫目录
            "auto_scan_assets": False,
        },
    },
    {
        "id": "miku_v5",
        "name": "初音ミク · 新模型",
        "short": "新模型",
        "note": "moc3 v5 · 6 张 4096² 贴图 · 全身取景 · 8 张表情",
        "dir": config.BASE_DIR / "models" / "miku_v5",
        "model3": "miku.model3.json",
        "profile": {
            # 这个模型的 model3.json 里 Motions 是空的，只有一个散装的
            # Scene1.motion3.json（VTube Studio 的 IdleAnimation）。
            # 所以所有情绪都走 Idle 组，情绪靠表情 + 头部倾角表达。
            "emotion_motion": {e: "Idle" for e in EMOTIONS},
            # 名字来自模型自带的 miku.vtube.json 热键表（VTS 里的按键名）。
            # 刻意不包含「水印」：那个 exp3 只是把 Param137 加 1，
            # 而表情在切换情绪时会被整体重置，水印就会重新冒出来。
            "emotion_expression": {
                "HAPPY": "比心", "SURPRISED": "圈圈", "MOTIVATED": "唱歌",
                "EMPATHY": "脸红", "ANGRY": "前倾", "SAD": None, "NORMAL": None,
            },
            # 该模型有标准名 ParamAngleZ / ParamBodyAngleZ，可以靠倾角表情绪。
            # 数值沿用手机端实测过的那一套（见 android/.../web/index.html）。
            "emotion_tilt": _tilt(-6, 8, 5, -9, -4, 7),
            # 水印：Param137 = 1 隐藏、0 显示（实测标定：=1 时画面纯白像素 8 个，
            # =0 时 1897~2781 个）。模型自然状态是 0，即默认**显示**。
            "watermark_param": "Param137",
            "watermark_hidden_value": 1.0,
            "watermark_shown_value": 0.0,
            # 标准名 ParamBreath，SetAutoBreathEnable 有效，不要再手动叠加
            "manual_breath": False,
            # model3.json 里 Expressions / Motions 都是空的，必须扫目录补装
            "auto_scan_assets": True,
        },
    },
]

_BY_ID = {m["id"]: m for m in MODELS}

# 默认模型：新模型（贴图更细、全身）
DEFAULT_ID = "miku_v5"


def ids() -> list[str]:
    return [m["id"] for m in MODELS]


def get(model_id: Optional[str]) -> Optional[dict]:
    """按 id 取模型；未知 id 返回 None（调用方负责回退）。"""
    if not model_id:
        return None
    return _BY_ID.get(model_id)


def resolve(model_id: Optional[str]) -> dict:
    """按 id 取模型，找不到就回退到默认 —— 永远返回一个可用的条目。

    为什么要回退而不是抛异常：模型目录是**运行时**才存在的（models/ 不入库），
    用户完全可能只下载了一个模型。这时候桌宠应该照常用另一个模型起来，
    而不是直接崩掉。
    """
    return get(model_id) or _BY_ID.get(DEFAULT_ID) or MODELS[0]


def available() -> list[dict]:
    """清单 + 「文件是否真的在」的标记，供控制台显示与手机端选择。"""
    out = []
    for m in MODELS:
        path = Path(m["dir"]) / m["model3"]
        out.append({
            "id": m["id"],
            "name": m["name"],
            "short": m["short"],
            "note": m["note"],
            "present": path.exists(),
            "model3": m["model3"],
        })
    return out


def model_json_path(model_id: Optional[str]) -> Path:
    m = resolve(model_id)
    return Path(m["dir"]) / m["model3"]


# ------------------------------------------------------------------ 当前选择
def _read_prefs() -> dict:
    try:
        data = json.loads(PREFS_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        # 文件不存在 / 坏了都当「还没选过」，不要让桌宠起不来
        return {}


def _write_prefs(data: dict) -> None:
    try:
        PREFS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PREFS_PATH.write_text(
            json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except Exception as exc:  # noqa: BLE001
        print(f"[Model] 保存模型选择失败：{exc}")


def selected(role: str) -> str:
    """取某个角色（desktop / phone）当前选的模型 id，永远返回有效 id。"""
    value = _read_prefs().get(role)
    return value if value in _BY_ID else DEFAULT_ID


def select(role: str, model_id: str) -> bool:
    """设置某个角色选的模型；返回是否真的发生了变化。"""
    if model_id not in _BY_ID:
        return False
    if selected(role) == model_id:
        return False
    data = _read_prefs()
    data[role] = model_id
    _write_prefs(data)
    return True
