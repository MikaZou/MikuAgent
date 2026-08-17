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
  const STT_KEY = "miku_stt_enabled";
  const DEFAULT_PLACEHOLDER = "和 Miku 说点什么吧…";
  const els = {
    canvas: $("live2d-canvas"),
    fallback: $("miku-fallback"),
    bubble: $("speech-bubble"),
    quickInput: $("quick-input"),
    inputBox: $("input-box"),
    btnSend: $("btn-send"),
    btnMic: $("btn-mic"),
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
    micStarted: false,
    recording: false,
    releasePending: false,
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

  /* ---------- 语音输入（按住说话） ---------- */
  function isSttEnabled() {
    return localStorage.getItem(STT_KEY) !== "0";
  }

  function setMicButtonVisible() {
    els.btnMic.hidden = !isSttEnabled();
  }

  function setMicUI(on) {
    els.btnMic.classList.toggle("recording", on);
    els.btnMic.title = on ? "松开结束" : "按住说话";
    els.inputBox.placeholder = on ? "聆听中…松开结束" : DEFAULT_PLACEHOLDER;
  }

  function voiceBubble(text, emotion) {
    setEmotion(emotion || "SAD");
    showBubble(text, emotion || "SAD");
    setTimeout(hideBubble, 4000);
  }

  function startVoiceInput() {
    if (!isSttEnabled() || state.recording) return;
    state.recording = true;
    state.micStarted = false;
    state.releasePending = false;
    setMicUI(true);
    api("/api/mic/start", { method: "POST" })
      .then((data) => {
        if (!state.recording) {
          // 用户在启动完成前已松开或取消，放弃这次录音
          api("/api/mic/cancel", { method: "POST" }).catch(() => {});
          return;
        }
        if (!data.ok) {
          state.recording = false;
          setMicUI(false);
          voiceBubble(data.message || "麦克风不可用，检查一下是否被占用？", "SAD");
          return;
        }
        state.micStarted = true;
        if (state.releasePending) {
          state.releasePending = false;
          stopVoiceInput();
        }
      })
      .catch((err) => {
        console.error("[MikuAgent] 麦克风启动失败：", err);
        state.recording = false;
        setMicUI(false);
        voiceBubble("语音输入不可用，看看后端服务是否在运行？", "SAD");
      });
  }

  function stopVoiceInput() {
    state.recording = false;
    state.micStarted = false;
    state.releasePending = false;
    setMicUI(false);
    api("/api/mic/stop", { method: "POST" })
      .then((data) => {
        if (state.recording) return; // 用户已开始新一轮录音
        const text = (data.text || "").trim();
        if (text) {
          els.inputBox.value = text;
          if (!state.busy) send();
        } else if (data.error) {
          voiceBubble(data.error, "SAD");
        }
      })
      .catch((err) => {
        console.error("[MikuAgent] 语音转写失败：", err);
        voiceBubble("转写失败了，再试一次？", "SAD");
      });
  }

  function cancelVoiceInput() {
    if (!state.recording) return;
    state.recording = false;
    state.micStarted = false;
    state.releasePending = false;
    setMicUI(false);
    api("/api/mic/cancel", { method: "POST" }).catch(() => {});
  }
  /* ---------- 设置 ---------- */
  async function renderSettings() {
    const health = await api("/api/health");
    const name = localStorage.getItem("miku_user_name") || "";
    const sttOn = isSttEnabled();
    const sttStatusText = {
      ready: `就绪（${health.stt.model}）`,
      loading: "模型加载中（首次使用需联网下载）",
      error: "加载失败，请看后端日志",
    }[health.stt.status] || health.stt.status;
    els.settingsBody.innerHTML = `
      <div class="row"><span>运行模式</span><span class="val">${health.mock ? "演示模式" : "已连接 DeepSeek"}</span></div>
      <div class="row"><span>API Key</span><span class="val">${health.has_api_key ? "已配置 ✓" : "未配置 ✗"}</span></div>
      <div class="row"><span>模型</span><span class="val">${health.model}</span></div>
      <div class="row"><span>🎤 语音输入（按住说话）</span>
        <label class="switch"><input type="checkbox" id="toggle-stt" ${sttOn ? "checked" : ""}><span class="slider"></span></label>
      </div>
      <div class="row"><span>语音引擎</span><span class="val">${sttStatusText}</span></div>
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
    $("toggle-stt").addEventListener("change", (e) => {
      localStorage.setItem(STT_KEY, e.target.checked ? "1" : "0");
      setMicButtonVisible();
      if (!e.target.checked && state.recording) cancelVoiceInput();
      showBubble(e.target.checked ? "语音输入已开启，按住 🎤 和我说话吧" : "语音输入已关闭", "HAPPY");
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
    els.btnMic.addEventListener("pointerdown", (e) => {
      e.preventDefault();
      try { els.btnMic.setPointerCapture(e.pointerId); } catch (_) {}
      startVoiceInput();
    });
    els.btnMic.addEventListener("pointerup", () => {
      if (!state.recording) return;
      if (state.micStarted) stopVoiceInput();
      else state.releasePending = true;
    });
    els.btnMic.addEventListener("pointercancel", cancelVoiceInput);
    els.btnMic.addEventListener("contextmenu", (e) => e.preventDefault());
    window.addEventListener("blur", cancelVoiceInput);
    setMicButtonVisible();
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
