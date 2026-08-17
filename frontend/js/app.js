/* MikuAgent 桌宠主逻辑：悬停输入、气泡回复、设置。 */
(function () {
  "use strict";

  const EMOTION_LABELS = {
    HAPPY: "开心", SAD: "难过", ANGRY: "生气", SURPRISED: "惊讶",
    MOTIVATED: "元气", EMPATHY: "温柔", NORMAL: "平静",
  };
  const EMOTION_ICONS = {
    HAPPY: "😄", SAD: "😢", ANGRY: "😠", SURPRISED: "😲",
    MOTIVATED: "💪", EMPATHY: "🥰", NORMAL: "😊",
  };

  const $ = (id) => document.getElementById(id);
  const els = {
    canvas: $("live2d-canvas"),
    fallback: $("miku-fallback"),
    bubble: $("speech-bubble"),
    quickInput: $("quick-input"),
    inputBox: $("input-box"),
    btnSend: $("btn-send"),
    btnClose: $("btn-close"),
    btnMinimize: $("btn-minimize"),
    btnSettings: $("btn-settings"),
    settingsPopup: $("settings-popup"),
    settingsBody: $("settings-body"),
    btnCloseSettings: $("btn-close-settings"),
  };

  const state = {
    sessionId: null,
    busy: false,
    inPyWebview: !!window.pywebview,
  };

  async function api(path, options = {}) {
    const resp = await fetch(path, {
      headers: { "Content-Type": "application/json" },
      ...options,
    });
    if (!resp.ok) {
      throw new Error(`请求失败 ${resp.status}`);
    }
    return resp.json();
  }

  /* ---------- 气泡 ---------- */
  function showBubble(text, emotion) {
    els.bubble.innerHTML = "";
    if (emotion && EMOTION_LABELS[emotion]) {
      const chip = document.createElement("span");
      chip.className = "emotion-chip";
      chip.textContent = `${EMOTION_ICONS[emotion]} ${EMOTION_LABELS[emotion]}`;
      els.bubble.appendChild(chip);
    }
    const content = document.createElement("span");
    content.className = "bubble-content";
    content.textContent = text || "";
    els.bubble.appendChild(content);
    els.bubble.hidden = false;
    els.bubble.scrollTop = 0;
  }

  function showTypingInBubble() {
    els.bubble.innerHTML = '<span class="typing-dots"><span></span><span></span><span></span></span>';
    els.bubble.hidden = false;
  }

  function hideBubble() {
    els.bubble.hidden = true;
  }

  async function typeInBubble(text, emotion) {
    showBubble("", emotion);
    const content = els.bubble.querySelector(".bubble-content");
    setSpeaking(true);
    let i = 0;
    while (i < text.length) {
      content.textContent = text.slice(0, i + 1);
      i += 1;
      await new Promise((resolve) => setTimeout(resolve, 16));
    }
    setSpeaking(false);
  }

  /* ---------- 对话 ---------- */
  async function send() {
    const message = els.inputBox.value.trim();
    if (!message || state.busy) return;
    els.inputBox.value = "";

    state.busy = true;
    els.btnSend.disabled = true;
    showTypingInBubble();

    try {
      const data = await api("/api/chat", {
        method: "POST",
        body: JSON.stringify({ message, session_id: state.sessionId }),
      });
      state.sessionId = data.session_id;
      setEmotion(data.emotion);
      await typeInBubble(data.reply, data.emotion);
    } catch (err) {
      console.error(err);
      setEmotion("SAD");
      await typeInBubble("呜…Miku 连不上后端了，主人看看服务是否在运行？", "SAD");
    } finally {
      state.busy = false;
      els.btnSend.disabled = false;
      els.inputBox.focus();
    }
  }

  /* ---------- 设置 ---------- */
  async function renderSettings() {
    const health = await api("/api/health");
    const name = localStorage.getItem("miku_user_name") || "";
    els.settingsBody.innerHTML = `
      <div class="row"><span>运行模式</span><span class="val">${health.mock ? "演示模式" : "已连接 DeepSeek"}</span></div>
      <div class="row"><span>API Key</span><span class="val">${health.has_api_key ? "已配置 ✓" : "未配置 ✗"}</span></div>
      <div class="row"><span>模型</span><span class="val">${health.model}</span></div>
      <div style="margin-top:10px">
        <label style="font-size:12px;color:var(--text-sub)">Miku 对你的称呼</label>
        <input id="nickname-input" type="text" placeholder="例如：主人 / 小名" value="${escAttr(name)}">
        <button id="btn-save-name">保存称呼</button>
      </div>
      <div class="tip">💡 在 .env 中配置 <b>DEEPSEEK_API_KEY</b> 后重启即可接入 DeepSeek 大脑。未配置时 Miku 用本地演示回复。</div>
    `;
    $("btn-save-name").addEventListener("click", async () => {
      const value = $("nickname-input").value.trim();
      localStorage.setItem("miku_user_name", value);
      await api("/api/meta", {
        method: "POST",
        body: JSON.stringify({ key: "user_name", value }),
      });
      showBubble("记住啦，主人～之后我就这样叫你哦☆", "HAPPY");
      setTimeout(hideBubble, 4000);
    });
  }

  function escAttr(value) {
    return String(value).replace(/"/g, "&quot;").replace(/</g, "&lt;");
  }

  /* ---------- 桌宠窗口控制（最小化/退出） ---------- */
  async function callPet(action) {
    if (window.pywebview && window.pywebview.api && typeof window.pywebview.api[action] === "function") {
      try {
        await window.pywebview.api[action]();
      } catch (err) {
        console.error(`[MikuAgent] ${action} 失败：`, err);
      }
      return;
    }
    if (action === "close" && !window.pywebview) {
      window.close();
    }
  }

  /* ---------- pywebview 透明桌宠模式检测 ----------
     pywebview 在页面加载完成后（NavigationCompleted）才注入 window.pywebview，
     因此需要在加载时、pywebviewready 事件、以及延迟兜底三个时机都做检测。 */
  function applyPetMode() {
    if (state.inPyWebview || document.body.classList.contains("pet-mode")) return;
    state.inPyWebview = true;
    document.documentElement.classList.add("pet-mode");
    document.body.classList.add("pet-mode");
  }

  // 支持 ?pet=1 在普通浏览器中预览透明桌宠效果
  if (window.pywebview || new URLSearchParams(location.search).has("pet")) applyPetMode();
  window.addEventListener("pywebviewready", applyPetMode);
  setTimeout(() => {
    if (window.pywebview) applyPetMode();
  }, 1500);

  /* ---------- 初始化 ---------- */
  async function init() {
    els.btnSend.addEventListener("click", send);
    els.btnClose.addEventListener("click", () => callPet("close"));
    els.btnMinimize.addEventListener("click", () => callPet("minimize"));
    els.inputBox.addEventListener("keydown", (e) => {
      if (e.key === "Enter" && !e.isComposing) send();
    });
    els.btnSettings.addEventListener("click", () => {
      els.settingsPopup.hidden = !els.settingsPopup.hidden;
      if (!els.settingsPopup.hidden) renderSettings().catch(console.error);
    });
    els.btnCloseSettings.addEventListener("click", () => {
      els.settingsPopup.hidden = true;
    });

    // 触屏设备：点击 Miku 切换输入栏显示（桌面端由 CSS :hover 控制）
    if (window.matchMedia("(hover: none)").matches) {
      els.canvas.addEventListener("click", () => {
        els.quickInput.classList.toggle("show");
      });
      els.fallback.addEventListener("click", () => {
        els.quickInput.classList.toggle("show");
      });
    }

    // 自动选择或新建会话（历史与记忆仍保存在后端，UI 不展示）
    try {
      const sessions = await api("/api/sessions");
      if (sessions.sessions.length) {
        state.sessionId = sessions.sessions[0].id;
      } else {
        const created = await api("/api/sessions", { method: "POST" });
        state.sessionId = created.id;
      }
    } catch (err) {
      console.error("[MikuAgent] 会话初始化失败：", err);
    }

    const ok = await initLive2D(els.canvas, els.fallback);
    if (ok) {
      showBubble("主人你好呀！我是初音ミク☆ 把鼠标移到我身上就能和我说话啦～", "HAPPY");
      setEmotion("HAPPY");
      setTimeout(hideBubble, 8000);
    } else {
      showBubble("Live2D 加载失败，已切换到图片模式…", "SAD");
    }
  }

  init().catch((err) => {
    console.error("[MikuAgent] 初始化失败：", err);
  });
})();
