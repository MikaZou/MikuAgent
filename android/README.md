# MikuAgent Android 客户端（路线 A 手机端）

PC 上的桌宠当服务端，手机当瘦客户端：**手机只做渲染 + 采集 + 播放，AI 逻辑全在 PC**。

完整设计见 [`docs/ANDROID_PLAN.md`](../docs/ANDROID_PLAN.md)，这里只讲怎么跑起来。

---

## 为什么不是「手机浏览器打开一个网址」

原来的 `web/phone.html` 走不通，原因不是网络，是浏览器的硬限制：

| 问题 | 后果 |
|---|---|
| 麦克风/摄像头用 `getUserMedia`，而 `http://192.168.x.x` **不是安全上下文** | `navigator.mediaDevices` 是 `undefined`，按住说话直接抛错 |
| 页面把 `connect()` 写在 Live2D 初始化**之后** | CDN 或模型任一失败，连文字聊天都一起死 |
| 依赖 jsdelivr CDN 与 `cubism.live2d.com` | 外部脆弱依赖 |
| 浏览器自动播放策略 | 语音被拦 |

旁证：PC 端 `data/remote.log` 里 65 行全是启动播报，「对话/转写/语音」记录 **0 条** ——
手机端从来没有成功交互过一次。

## 这一版怎么解决的

```
WebView（只渲染，不碰网络）
   ↑ shouldInterceptRequest 喂本地文件
Kotlin：WebSocket / 模型同步 / 录音 / 播放 / 口型包络 / 相机
```

- 页面来自 `https://appassets.androidplatform.net`（`WebViewAssetLoader` 的本地源）
  → **安全上下文**，同时不发任何外部请求 → 零 CDN
- WebSocket 在 Kotlin 里开 → 不受 `https → ws://` 的 mixed-content 限制
- 录音、播放、相机都是原生 → 完全绕过浏览器限制

---

## 环境要求

| 组件 | 版本 | 说明 |
|---|---|---|
| JDK | 17 或 21 | 本机用 21 |
| Android SDK | platform 35 / build-tools 35.0.0 | `ANDROID_HOME=D:\Android\Sdk` |
| Gradle | 8.9 | |
| AGP | 8.6.1 | |
| Kotlin | 2.0.21 | |
| 最低 Android | 8.0 (API 26) | |

### ⚠️ 国内网络必须配镜像

实测 **`repo1.maven.org`（Maven Central）与 `maven.google.com` 都不通**。
`settings.gradle.kts` 里已经把阿里云镜像排在前面，别删：

```kotlin
maven("https://maven.aliyun.com/repository/google")
maven("https://maven.aliyun.com/repository/public")
```

`local.properties` 里的 `sdk.dir` 也要对：

```properties
sdk.dir=D\:\\Android\\Sdk
```

---

## 构建

```bash
cd android
gradle assembleDebug          # 或者用 Android Studio 打开本目录直接 Run
# 产物：app/build/outputs/apk/debug/app-debug.apk
```

安装：

```bash
adb install -r app/build/outputs/apk/debug/app-debug.apk
```

## 首次使用

1. **PC 端**：启动桌宠（`start.bat` 或 `python main.py`）。
   气泡里会显示手机该访问的地址，形如 `192.168.20.102`。
   也可以在 `data/remote.log` 里看。
2. **手机**：打开 App，填 PC 的地址，点「连接」。
   - **安卓模拟器**填 `10.0.2.2`（模拟器访问宿主机的固定地址）
   - **真机**填 PC 的局域网 IP，且两者要在同一个 WiFi
3. 首次连接会自动把模型同步到手机（约 35 MB）。

---

## 模型为什么不打包进 APK

模型（`D:\game\miku`）的说明文件明确写着 **「不可二传二改」**。

所以：**APK 里不含模型**，模型在首次连接时从**你自己的 PC** 同步到手机的私有目录
（`filesDir/miku_v5/`）。传输只发生在你的 PC 和你自己的手机之间，不经过第三方。
同理，仓库的 `.gitignore` 也把 `models/` 排除了。

## 贴图显存：手机端的头号风险

新模型是 **6 张 4096×4096** 贴图，未压缩 RGBA 要 **384 MB 显存**。

`AssetServer` 支持在原生侧用 `BitmapFactory.inSampleSize` 把贴图缩小后再交给 WebGL：

- `textureScale = 1.0` → 4096²，384 MB（默认，先在真机上试）
- `textureScale = 0.5` → 2048²，约 96 MB

页面报告 `RENDER_FAILED` 且原因是 WebGL 上下文丢失时，`MainActivity` 会
**自动降到 0.5 重试一次**。

## 排查

```bash
adb logcat -s MikuAgent:* MikuJS:* AssetServer:* RemoteClient:* ModelSync:*
```

关键行：

- `RENDER_READY {...}` —— 渲染成功的自证信息（Core 版本、Drawable 数、贴图数、加载耗时）
- `RENDER_FAILED ...` —— 渲染失败原因
- `模型就绪：下载 N 个，跳过 M 个`
- `已连接 ws://...`

## 当前进度与未完成项

已完成：环境搭建、模型解包与校验、PC 侧模型服务、渲染管线、原生 I/O、口型包络。

未完成（见计划 §6）：

- **Phase 4**：表情/动作。新模型的 `model3.json` 里 `Expressions`/`Motions` 是空的，
  9 个表情是独立 `.exp3` 文件，要靠 `miku.vtube.json` 的热键表映射后在内存里补全。
  另外模型要求「水印表情默认打开」，也要在这一步做。
- **相机**：`Bridge` 里已有回传通道，`PhotoTaker` 待实现。
- 真机验收（384 MB 贴图在 iQOO 上的实际表现）。
