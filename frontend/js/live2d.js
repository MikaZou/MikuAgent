/* Live2D 初音未来：模型加载（Cubism 4）、情感动作/表情、口型模拟。 */
const MODEL_URL = "/live2d/miku/miku.model3.json";

let live2dModel = null;
let pixiApp = null;
let mouthFrameId = null;

// 情感 → 动作组 / 表情（对应 miku.model3.json 与表情和动作/）
const EMOTION_MOTION = {
  HAPPY: "Tap",
  ANGRY: "Flick",
  SURPRISED: "FlickUp",
  SAD: "Cry",
  MOTIVATED: "Tap",
  EMPATHY: "Idle",
  NORMAL: "Idle",
};

const EMOTION_EXPRESSION = {
  HAPPY: "Saihong",      // 腮红
  ANGRY: null,           // 生气用动作表现
  SURPRISED: "Chijing",  // 吃惊
  SAD: null,
  MOTIVATED: "Dazhihui",
  EMPATHY: "Mimiyan",    // 眯眯眼
  NORMAL: null,
};

async function initLive2D(canvas, fallbackImg) {
  try {
    if (!window.PIXI || !window.PIXI.live2d || typeof PIXI.live2d.Live2DModel !== "function") {
      throw new Error("Live2D 库未加载（pixi / cubism4 / live2dcubismcore）");
    }

    const app = new PIXI.Application({
      view: canvas,
      width: 420,
      height: 560,
      backgroundAlpha: 0,
      antialias: true,
      autoDensity: true,
      resolution: window.devicePixelRatio || 1,
    });
    pixiApp = app;

    const model = await PIXI.live2d.Live2DModel.from(MODEL_URL, { autoInteract: false });
    live2dModel = model;

    const scale = Math.min(400 / model.width, 540 / model.height);
    model.scale.set(scale);
    model.anchor.set(0.5, 1);
    model.x = 210;
    model.y = 550;
    app.stage.addChild(model);

    // 鼠标注视跟随
    window.addEventListener("mousemove", (e) => {
      if (live2dModel && typeof live2dModel.focus === "function") {
        live2dModel.focus(e.clientX, e.clientY);
      }
    });

    // 点击 Miku：随机动作（app.js 同时负责切换输入栏显示）
    canvas.addEventListener("click", () => {
      const motions = ["Tap", "Tap", "Flick", "FlickUp", "Dance", "Idle"];
      playMotion(motions[Math.floor(Math.random() * motions.length)]);
    });

    document.body.dataset.live2d = "ok";
    return true;
  } catch (err) {
    console.error("[Live2D] 加载失败：", err);
    if (canvas) canvas.hidden = true;
    if (fallbackImg) fallbackImg.hidden = false;
    return false;
  }
}

function playMotion(group) {
  if (!live2dModel || typeof live2dModel.motion !== "function") return;
  try {
    live2dModel.motion(group);
  } catch (err) {
    console.warn("[Live2D] 动作触发失败：", err);
  }
}

function setEmotion(emotion) {
  if (!live2dModel) return;
  playMotion(EMOTION_MOTION[emotion] || "Idle");

  const expression = EMOTION_EXPRESSION[emotion];
  try {
    if (expression && typeof live2dModel.expression === "function") {
      live2dModel.expression(expression).catch(() => {});
    } else if (typeof live2dModel.expressionManager?.resetExpression === "function") {
      live2dModel.expressionManager.resetExpression();
    }
  } catch (err) {
    console.warn("[Live2D] 表情切换失败：", err);
  }

  // 生气时手动压低眉毛（模型参数名为大写 PARAM_*）
  if (emotion === "ANGRY") {
    try {
      const core = live2dModel.internalModel.coreModel;
      core.setParameterValueById("PARAM_BROW_L_Y", -1);
      core.setParameterValueById("PARAM_BROW_R_Y", -1);
    } catch (_) {}
  }
}

function setSpeaking(on) {
  if (!live2dModel) return;
  if (on) {
    if (mouthFrameId) cancelAnimationFrame(mouthFrameId);
    const start = performance.now();
    const tick = (t) => {
      if (!live2dModel) return;
      const elapsed = t - start;
      const value = Math.max(
        0,
        Math.min(1, (Math.sin(elapsed * 0.012) + Math.sin(elapsed * 0.031) * 0.4 + 0.8) * 0.5)
      );
      try {
        live2dModel.internalModel.coreModel.setParameterValueById("ParamMouthOpenY", value);
      } catch (_) {}
      mouthFrameId = requestAnimationFrame(tick);
    };
    mouthFrameId = requestAnimationFrame(tick);
  } else {
    if (mouthFrameId) {
      cancelAnimationFrame(mouthFrameId);
      mouthFrameId = null;
    }
    try {
      live2dModel.internalModel.coreModel.setParameterValueById("ParamMouthOpenY", 0);
    } catch (_) {}
  }
}
