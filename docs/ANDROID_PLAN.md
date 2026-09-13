# 安卓端 Live2D 应用（路线 A 手机客户端）实施计划

> **状态**：✅ **已执行完成**（真机验收通过，提交 `061d47c`）
> **目标读者**：后续执行该计划的工程师 / 未来的自己
> **前置阅读**：`docs/TECHNICAL.md` 5.4（手机版可行性）、5.4.5（路线 A）、
> 5.6（浏览器版，已被取代）、**5.9（原生客户端落地结果）**
>
> 本文所有「实测」数字都是在开发机上真实跑出来的，不是估算。执行时若发现与本文不符，以实测为准并回来更新本文。

> **落地说明（计划执行后补充）**：主体按本计划完成，但**客户端形态在执行中改了** ——
> 原计划是基于浏览器（`web/phone.html`）继续做，实际发现
> `http://192.168.x.x` **不是安全上下文**、`navigator.mediaDevices` 直接是
> `undefined`，浏览器里麦克风与摄像头**根本无法使用**，于是改为**原生 Android 应用**。
> 详细结果与所有实测数据见 `docs/TECHNICAL.md` **§5.9**。

---

## 0. 验收结果（执行后逐条回填）

| # | 标准 | 结果 | 证据 |
|---|---|---|---|
| 1 | APK 装到 iQOO 上，连上 `ws://<PC>:8765/ws`，显示「已连接」 | ✅ | `RemoteClient: 已连接 ws://10.150.224.155:8765/ws` |
| 2 | 新模型（moc3 v5，6 张贴图）完整渲染 | ✅ | `RENDER OK textures=6 texSizes=[4096x4096 ×3] parts=77 params=141 drawables=440`；截图确认全身居中 |
| 3 | 打字聊天 → 气泡 + 语音 + 口型 | ✅ | 帧内探针实测口型 469 帧内 0~0.59、69 个不同值 |
| 4 | 按住说话 → PC 转写 → 回复 + 语音 | ✅ | `收到语音 2.68s 峰值=0.468` → `转写 -> '你现在能看得到我吗？初音殿下。'` → `语音 10.10s / 631KB` |
| 5 | 拍照发送 → PC 端视觉对话收到画面 | ✅ | `对话 3.29s (带图=True)`；她回复「看到啦～主人戴着眼镜，头发翘起来一点点」 |
| 6 | `data/remote.log` 出现「对话 / 转写 / 语音」记录 | ✅ | 此前 0 条；修掉「交互日志不落盘」后三类均有记录 |
| 7 | 模型加载后不 OOM、不闪退 | ✅ | `GL mtrack 560MB`、`Graphics 627MB`、`TOTAL 736MB`，真机 11.7GB 内存无压力 |

**超出原计划完成的部分**：视频对话（与 PC 同方案：说话时附一帧）、水印开关、
每日会话三端共享、表情/动作内存补全、Android Studio 环境搭建。

**仍未完成 / 已知问题**：

- **跨零点自动切天**只做了逻辑级验证（模拟「昨天的会话」），没在真实午夜跑过
- **桌面端取景仍是硬编码常量**（`FRAMING_SCALE`/`OFFSET`，照旧模型量的），
  没有像 Android 端那样改成按美术包围盒自适应
- `data/` 里 09-14 有 11 条改动前分裂出的碎片会话（多为测试产生），未清理

---

## 1. 目标与成功标准

做一个**可安装的 Android APK**，替代现在用浏览器打开的 `web/phone.html`，作为路线 A 的手机瘦客户端：只做「渲染 + 采集 + 播放」，AI 逻辑仍全部在 PC。

### 功能验收标准（每条都可实测判定）

| # | 标准 |
|---|---|
| 1 | APK 装到 iQOO 上打开，自动连上 PC 的 `ws://<PC>:8765/ws`，界面显示「已连接」 |
| 2 | **新模型（`D:\game\miku`，moc3 v5，6 张贴图）完整渲染**，不是空白/报错 |
| 3 | 打字聊天 → 气泡显示回复 + 听到语音 + 口型跟着动 |
| 4 | 按住说话 → PC 转写 → 回复 + 语音（这是现在完全用不了的功能） |
| 5 | 拍照发送 → PC 端视觉对话收到画面 |
| 6 | `data/remote.log` 出现「对话 / 转写 / 语音」记录（**当前实测是 0 条**） |
| 7 | 模型加载后不 OOM、不闪退 |

---

## 2. 已核实的关键事实（决策依据）

| 事实 | 值 | 影响 |
|---|---|---|
| 新模型 moc3 版本 | **v5**（`moc3` 头 `MOC3\x05`） | 排除只支持 Cubism 4 的老渲染器 |
| 旧模型 moc3 版本 | v4（0.35 MB，单贴图） | 新旧模型分属两代格式 |
| 新模型贴图 | **6 × 4096²**，PNG 合计 25 MB | **RGBA 显存 384 MB**，手机上的主要风险 |
| 新模型参数 | 141 个，口型参数 = **`ParamMouthOpenY`** | 口型方案可从 PC 平移 |
| 新模型 `LipSync` 参数组 | **0 个参数**（cdi3 的 ParameterGroups 也全空） | 不能用「参数组」机制，必须**按 ID 直接驱动** |
| 新模型 `model3.json` | Motions / Expressions **都是空** | 表情/动作不在标准位置，见 §4.4 |
| 表情来源 | `miku.vtube.json` 的 **9 条热键**：圈圈 / 脸红 / 前倾 / 葱 / 唱歌 / 比心 / QQ人 / 水印 / RemoveAllExpressions | 需按 VTS 热键表映射 |
| **Cubism Core for Web 版本** | **5.1.0.0**（`csmGetVersion()` = 83951616） | **能读 moc3 v5** |
| PC 端 live2d-py 的 Native Core | 日志 `05.01.0000 (83951616)` | **与 Web Core 同版本**，技术同源 |
| Core JS 授权 | 文件头注明是 "Redistributable Code" | 可随 APK 分发 |
| `data/remote.log` | 65 行全是启动播报，对话/转写/语音 **0 条** | 手机端从没成功交互过 |

### 2.1 现有手机端失败的机理（已定位，不是网络问题）

1. **麦克风/摄像头走 `getUserMedia`，而 `http://192.168.x.x` 不是安全上下文**
   → `navigator.mediaDevices` 是 `undefined`，一调用就抛 `TypeError`。
2. `phone.html` 把 `connect()` 写在 `initLive2D()` **之后**
   → CDN 或模型任一失败就 `return`，**连文字聊天都一起死掉**。
3. 依赖 jsdelivr CDN（`REMOTE_CDN` 默认 `https://cdn.jsdelivr.net/npm`）+ 硬编码 `cubism.live2d.com`
   → 实测这两个当前可达，但属外部脆弱依赖。
4. 浏览器自动播放策略会拦截语音（代码里已有「浏览器拦截了自动播放」的兜底提示）。

### 2.2 开发机环境实测

| 项 | 状态 |
|---|---|
| JDK | ✅ 21.0.7，`JAVA_HOME` 已设 |
| IntelliJ IDEA | ✅ Community 2025.1.1（**注意：2025.1 已不再捆绑安卓支持**，需另装插件） |
| Visual Studio | ✅ 2022 17.7.1（NDK 回退路线可用） |
| Node | ✅ v24.12.0 |
| Android SDK / adb / Gradle / Android Studio | ❌ **全部没有，要从零装** |
| 磁盘 | C 盘 111 GB / D 盘 100 GB 可用 |
| 内存 | 15.7 GB，但**常态只剩 2.7 GB 可用**（桌宠 + 浏览器在占） |
| 虚拟化 | `HypervisorPresent=True`，但 `VirtualizationFirmwareEnabled=False`；查询 Hyper-V 功能被拒（需管理员） |
| 已有安卓设备连线 | ❌ 当前无 |
| `D:\Android` 目录 | ⚠️ 是加密狗 SDK，**与安卓开发无关** |

### 2.3 网络可达性实测（决定构建能否成功）

| 目标 | 结果 |
|---|---|
| `dl.google.com`（SDK / Maven） | ✅ HTTP 200 |
| `maven.google.com` | ❌ 不通（但 `dl.google.com/dl/android/maven2` 通，HTTP 200） |
| **`repo1.maven.org`（Maven Central）** | ❌ **不通 → 必须配镜像** |
| `maven.aliyun.com` | ✅ HTTP 200（google / public 两个仓库都实测 200） |
| `mirrors.cloud.tencent.com` | ✅ 可连 |
| `services.gradle.org` | ✅ 可连，gradle-8.9-bin.zip 129.8 MB |
| Android Studio 安装包 | ✅ HTTP 200，**1439 MB** |
| `commandlinetools-win-*.zip` | ✅ HTTP 200，153 MB |
| GitHub HTTPS | ❌ 被阻断（DNS 通、TCP 通、TLS 被 reset），三次推送均超时 |
| GitHub SSH | ✅ 可用（`ssh -T git@github.com` 认证成功；Live2D 官方仓库可 `ls-remote`） |
| `download.cubism.live2d.com` | ❌ 不通 |

---

## 3. 技术选型

### 选定：WebView 只负责渲染 + Kotlin 负责全部 I/O（混合架构）

| 方案 | 结论 |
|---|---|
| **WebView 渲染 + Kotlin 原生 I/O** | ✅ **采用**。Core 5.1.0.0 与 PC 同版本、能读 moc3 v5、无授权门槛、复用已验证的 pixi-live2d-display 渲染；Kotlin 接管 WS/录音/播放/相机后，安全上下文与 mixed-content 限制**全部消失** |
| 全原生 Kotlin + Cubism SDK for Native | ❌ 本次不采用。官方 Native SDK 的 **Core 必须在 live2d.com 勾选同意授权后才能下载**（`download.cubism.live2d.com` 不可达），这是用户本人的法律动作；且要自己写 JNI + OpenGL ES 渲染管线，工作量 ×3~5 |
| 继续用浏览器 | ❌ 已证伪，安全上下文是浏览器硬限制，绕不过去 |

**关键点**：WebView 只当渲染器用，**不碰网络**——页面加载自 `https://appassets.androidplatform.net`（`WebViewAssetLoader` 提供的本地资源，天然安全上下文），模型资源由 Kotlin 拦截后喂进去。这样 `ws://` 和 `http://` 的 mixed-content 规则都不适用。

### 回退路径（若 Phase 1 spike 失败）

1. 换 `@naari3/pixi-live2d-display` 或 `pixi-live2d-display-lipsyncpatch` 分支
2. 仍不行 → 转 Cubism SDK for Native
   - `CubismNativeFramework` / `CubismNativeSamples` 仓库**已实测可用 SSH 克隆**
   - 但 **Core 需用户本人**到 <https://www.live2d.com/en/sdk/download/native/> 同意授权后下载

---

## 4. 总体架构与数据流

### 4.1 分层

```
┌─────────────────────── Android APK ───────────────────────┐
│  WebView（只渲染，无网络）                                  │
│    index.html + pixi.min.js + cubism4.min.js + Core5.1.js  │
│    ← assets/web/（打包进 APK，零 CDN）                      │
│         ↑ shouldInterceptRequest 喂本地文件                 │
│  Kotlin 层                                                  │
│    · RemoteClient   : WebSocket  → ws://<PC>:8765/ws        │
│    · AudioCapture   : AudioRecord → 16k 单声道 WAV          │
│    · AudioPlayer    : AudioTrack  播放 + RMS 包络           │
│    · ModelSync      : HTTP 从 PC 拉模型 → 私有目录缓存      │
│    · AssetServer    : 拦截 /model/* → 本地文件（可降采样）   │
│    · CameraX        : 拍照 → JPEG → base64                  │
│    · Bridge         : @JavascriptInterface + evalJs         │
└────────────────────────────────────────────────────────────┘
                            ↕ 局域网
┌──────────────────── PC（现有 Python，协议不变）────────────┐
│  remote_server.py : /ws  /health  /model/  /static/         │
│  agent / tts / stt / memory                                 │
└────────────────────────────────────────────────────────────┘
```

### 4.2 消息流（与现有一致，**协议不改**）

| 方向 | 消息 |
|---|---|
| App→PC | `{"type":"chat","text":…,"image":<b64 jpeg 可选>,"session_id":…}` |
| App→PC | `{"type":"audio","data":<b64 wav>,"session_id":…}` |
| App→PC | `{"type":"ping"}` |
| PC→App | `ready` / `transcript` / `reply` / `speech` / `error` / `pong` |

`speech.data` 是 base64 WAV（32 kHz 单声道），Kotlin 解码后用 AudioTrack 播放。

### 4.3 口型同步（原生侧算，不依赖前端）

新模型 `LipSync` 参数组为空 → 必须按参数 ID 驱动。
Kotlin 播放音频时按 ~30 Hz 计算 RMS 包络，通过桥推到 JS，由 JS 写 `ParamMouthOpenY`。
（PC 端 `ui/live2d_view.py` 的 `set_param` 已用同样思路，方案可平移。）

### 4.4 表情与动作（新模型的特殊情况）

`model3.json` 的 Motions/Expressions 是空的，表情是 9 个独立 `.exp3.json`。方案：

- **磁盘文件一个都不改**（模型授权写明「不可二传二改」）
- Kotlin 的 `AssetServer` 在**内存里**补全 `miku.model3.json` 的 `Expressions` / `Motions` 段，
  映射表读模型自带的 `miku.vtube.json` 热键表
- `Scene1.motion3.json` 挂到 `Idle` 组
- **`水印.exp3.json` 默认应用**（模型说明第 4 条明确要求「水印按键默认打开」），并在设置里提供关闭入口

---

## 5. 阶段 0：开发环境搭建

| 步骤 | 内容 |
|---|---|
| 0.1 | 下载 Android Studio **2026.1.3.7**（1439 MB，已实测 HTTP 200）<br>`https://edgedl.me.gvt1.com/android/studio/install/2026.1.3.7/android-studio-quail3-windows.exe` |
| 0.2 | 安装到 **D 盘**（`D:\Android\Android Studio`），SDK 也放 D 盘（`D:\Android\Sdk`），设 `ANDROID_HOME` |
| 0.3 | 装 SDK 组件：`platform-tools`、`platforms;android-35`、`build-tools;35.0.0`、`cmdline-tools;latest`、`emulator`、`system-images;android-35;google_apis;x86_64` |
| 0.4 | **配阿里云 Maven 镜像**（关键：`repo1.maven.org` 实测不通）。写进 `settings.gradle.kts` 的 `pluginManagement` + `dependencyResolutionManagement` |
| 0.5 | Gradle wrapper：`services.gradle.org` 可达；如慢可换腾讯镜像 |
| 0.6 | 建一个 x86_64 AVD 用于日常验证 |
| 0.7 | 真机：用户需在 iQOO 上开启**开发者模式 + USB 调试**并插线（`adb devices` 确认） |

**模拟器风险**：`HypervisorPresent=True`，但 `VirtualizationFirmwareEnabled=False`，且 Hyper-V 功能查询需管理员权限。
若模拟器起不来，可能需要**以管理员身份启用「Windows 虚拟机监控程序平台」并重启** —— 这是用户动作。

---

## 6. 分阶段实施

### Phase 1：渲染兼容性 spike（**最大风险，先做**）

最小可运行工程，只验证一件事：**Core 5.1 + pixi-live2d-display 能不能加载这个 moc3 v5 模型**。

- 一个 Activity + WebView，加载本地 `index.html`
- 模型文件先手动推送到设备（`adb push` 到 `/sdcard/Download/miku_v5/`）
- 判定：模型完整显示、6 张贴图都加载、点击有反应
- **不通过就立刻转回退路径，不往下做**

### Phase 2：前端去 CDN 化 + 资源本地化

- 把 `web/phone.html` 拆成 `android/app/src/main/assets/web/index.html` + 本地 JS 库
- 打包 `pixi.min.js`(6.5.10)、`cubism4.min.js`(0.4.0)、`live2dcubismcore.min.js`(5.1.0.0) —— 全部下载后放进 assets
- 实现 `AssetServer.shouldInterceptRequest`：
  - `/model/*` → 应用私有目录里的模型缓存
  - **PNG 贴图可按 `textureScale` 用 `BitmapFactory.inSampleSize` 降采样**（4096→2048 可把 384 MB 压到 96 MB）
  - 内存里补全 `model3.json` 的 Expressions/Motions（见 §4.4）
  - **修掉「Live2D 失败就整站死掉」的设计**：先 `connect()` 再初始化渲染，渲染失败降级为「只聊天 + 立绘占位」
- PC 侧改动随本阶段一起做（见 §7）

### Phase 3：原生 I/O + JS 桥

- `RemoteClient`：WebSocket（OkHttp），断线 3 秒重连、心跳 ping
- `AudioCapture`：`AudioRecord` 16 kHz 单声道 → 写 WAV 头 → base64
- `AudioPlayer`：base64 → PCM → `AudioTrack`，同时算 RMS 包络
- `CameraX`：拍照 → JPEG → base64
- `Bridge`：
  - JS→Kotlin：`sendChat` / `startRecording` / `stopRecording` / `takePhoto` / `ping`
  - Kotlin→JS：`onReply` / `onTranscript` / `onStatus` / `onProvider` / `setMouth`
- 运行时权限：`RECORD_AUDIO` / `CAMERA`（原生权限，不再受浏览器限制）
- `AndroidManifest` 开 `usesCleartextTraffic`（局域网明文 HTTP/WS 是必要的）

### Phase 4：UI 与健壮性

- 气泡、状态点、引擎信息、按住说话按钮、拍照按钮
- 错误提示落到界面上（连不上 / 模型加载失败 / 合成失败）
- 后台切换、锁屏、旋转的处理

### Phase 5：验证与出包

- 模拟器 + 真机各跑一遍 §1 的 7 条验收
- 产出 debug APK；确认 `data/remote.log` 出现对话/转写记录
- 写 `android/README.md`（构建、安装、连不上时的排查）

---

## 7. 文件清单

### 新增（`android/`，与现有 Python 项目并列，可提交）

```
android/
  settings.gradle.kts            # 含阿里云镜像
  build.gradle.kts / gradle.properties / gradle/wrapper/
  app/build.gradle.kts
  app/src/main/AndroidManifest.xml
  app/src/main/java/com/mikuagent/pet/
      MainActivity.kt
      net/RemoteClient.kt
      audio/AudioCapture.kt  audio/AudioPlayer.kt
      model/ModelSync.kt     web/AssetServer.kt
      web/Bridge.kt          camera/PhotoTaker.kt
  app/src/main/assets/web/
      index.html  pixi.min.js  cubism4.min.js  live2dcubismcore.min.js
  app/src/main/res/...
  README.md
```

### PC 侧最小改动（Python，`backend/`）

- `config.py`：新增 `REMOTE_MODEL_DIR`（**默认等于现有模型目录**）
  —— 必须与 PC 桌宠自己的 `MODEL_PATH` **解耦**，否则一改就会把 PC 桌宠的渲染搞坏
- `remote_server.py`：`MODEL_DIR` 改用 `REMOTE_MODEL_DIR`；
  新增 `GET /model/manifest`（文件列表 + 大小 + 哈希），供 App 做增量同步与校验
- `.gitignore`：新增 `models/`、`android/**/build/`、`android/.gradle/`、`*.apk`

### 模型落地位置

把 `D:\game\miku\miku .rar` 解到 `D:\game\MikuAI\models\miku_v5\`（**gitignore，不进仓库**）。
解包用 Windows 自带 `tar`（**已实测能读该 RAR**）；若解包失败再装 7-Zip。

---

## 8. 边界情况与失败模式

| 场景 | 处理 |
|---|---|
| 贴图 384 MB 导致 WebGL 上下文丢失 / OOM | 自动降到 `textureScale=0.5`（2048²，96 MB）重试一次；再失败则提示并只保留聊天 |
| pixi-live2d-display 与 Core 5.1 不兼容 | Phase 1 就会暴露；按 §3 回退路径处理 |
| PC 不在线 / IP 变了 | 状态显示「未连接」，3 秒重连；提供手动输入 PC 地址的入口 |
| 模型文件不完整 / 哈希不符 | `manifest` 校验后重新拉取，避免用半截文件渲染 |
| 播放被系统静音 | 用 `AudioAttributes` 明确标注媒体用途；不依赖自动播放策略 |
| 录音权限被拒 | 明确提示并跳系统设置，不静默失败 |
| `ParamMouthOpenY` 不存在于某模型 | 参数解析做「按 ID → 按名字 → 放弃」三级降级（PC 端已有同类逻辑可参考） |
| 水印表情 | 默认应用，设置里可关（尊重模型作者要求） |

---

## 9. 测试与验收

- **渲染**：Phase 1 spike 通过 + 真机截图确认 6 张贴图无缺失
- **内存**：模型加载后 `adb shell dumpsys meminfo` 无异常增长；连续操作 5 分钟不 OOM
- **端到端**：模拟器 + 真机各跑一遍 §1 的 1~7 条
- **回归**：PC 端桌宠启动、说话、设置面板「修改配置」不受影响（因为只加了 `REMOTE_MODEL_DIR`）
- **日志证据**：`data/remote.log` 必须出现「对话 / 转写 / 语音」，这是现在 0 条、也是本次最硬的验收指标

---

## 10. 明确不在本次范围

- **PC 桌宠改用新模型**：`ui/live2d_view.py` 的动作/表情/参数映射是照着旧模型
  （moc3 v4，13 动作/6 组、`Saihong`/`Chijing`/`liuhan`）硬编码的，
  换成 141 参数的新模型是**另一块独立工作量**。
  本次只让**手机端**渲染新模型，PC 桌宠维持现状
  （因此 PC 侧必须用 `REMOTE_MODEL_DIR` 解耦）。这是自然的下一步，但不混在这次做。
- Live2D 生成功能本身（本次只预留参数级 API）。

---

## 11. 风险与回退

| 风险 | 概率 | 回退 |
|---|---|---|
| pixi-live2d-display 不兼容 Core 5.1 | 中 | 换维护中的分支；再不行转 Native SDK（需用户下载授权） |
| 384 MB 贴图在真机 OOM | 中 | 降采样到 2048²；仍不行降到 1024² |
| 模拟器起不来（WHPX 未启用） | 中 | 只用真机验证 |
| Android Studio 1.4 GB + SDK ~4 GB 下载慢 | 低 | 已确认全部源 200；必要时改用 cmdline-tools 纯命令行 + IntelliJ |
| 模型授权「不可二传二改」 | — | **不打进 APK、不提交进 git**，只在用户本机 `models/` 与手机私有目录存在 |

---

## 12. 需要用户做的动作

1. **同意把 Android Studio + SDK 装到 D 盘**（约 5.5 GB）
2. 真机验证时：在 iQOO 上开**开发者模式 + USB 调试**并插线
3. 若模拟器不可用：可能需要**以管理员身份启用 Windows 虚拟机监控程序平台并重启**
4. 确认模型使用范围：`D:\game\miku` 的模型说明写明
   「不可二传二改、严禁商用」，本计划据此**不打进 APK、不进 git**，
   仅本机与用户手机私有目录使用
5. 只有走 Native SDK 回退路径时，才需要用户本人到 live2d.com 同意授权并下载 SDK

---

## 13. 实施顺序小结

```
Phase 0 环境（Android Studio + SDK + 阿里云镜像 + AVD + 真机接线）
   ↓
Phase 1 渲染 spike（最大风险，不通过就换路线）
   ↓
Phase 2 去 CDN + 资源拦截（含贴图降采样、model3.json 内存补全）
   ↓
Phase 3 原生 WS / 录音 / 播放 / 口型 / 相机 + JS 桥
   ↓
Phase 4 UI 与健壮性
   ↓
Phase 5 模拟器 + 真机验收，出 APK
```

PC 侧 Python 改动（`REMOTE_MODEL_DIR` + `/model/manifest`）随 Phase 2 一起做，
改动面刻意压到最小以免影响现有桌宠。
