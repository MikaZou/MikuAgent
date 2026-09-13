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

## 在模拟器上跑（若干坑，都已实测）

```bash
# 1. 启动（AOSP 镜像比 google_apis 轻，推荐）
emulator -avd <名字> -no-snapshot -no-boot-anim -gpu host -memory 1536

# 2. 模拟器访问宿主机固定是 10.0.2.2（App 会自动填，无需手输）

# 3. 授权运行时权限（否则「按住说话」会卡在权限弹窗）
adb shell pm grant com.mikuagent.pet android.permission.RECORD_AUDIO
adb shell pm grant com.mikuagent.pet android.permission.CAMERA
```

### 坑 1：Chromium 把模拟器 GPU 拉黑名单，WebGL 直接不可用

报错 `ContextResult::kFatalFailure: WebGL1 blocklisted`，PIXI 会提示
`WebGL unsupported in this browser`。**真机不受影响**，是模拟器的
「Android Emulator OpenGL ES Translator」在 Chromium 的 GPU 黑名单里。

绕过办法是给 WebView 写命令行标志（官方支持的调试机制）：

```bash
adb shell "echo '_ --ignore-gpu-blocklist --enable-unsafe-swiftshader' > /data/local/tmp/webview-command-line"
adb shell chmod 644 /data/local/tmp/webview-command-line
```

写完重启 App 生效。这个文件在 AVD 的 userdata 里，重启模拟器仍在
（除非 `-wipe-data`）。

### 坑 2：`adb shell input tap` 打不中 WebView 里的 HTML 控件

WebView 内的 HTML 输入框拿不到焦点，`input text` 的字符会丢失，
点击也常被系统弹窗截走。端到端测试改用 **WebView DevTools 协议**
直接在页面上下文里执行 JS：

```bash
adb forward tcp:9222 localabstract:webview_devtools_remote_$(adb shell pidof com.mikuagent.pet)
# 然后连 ws://127.0.0.1:9222 的 webSocketDebuggerUrl，发 Runtime.evaluate
```

`.tmp/cdp_chat.py` 就是这个用途（`probe` / `chat` / `eval`）。

### 坑 3：开发机内存不够会直接把模拟器饿到 ANR

实测本机页面文件被**固定为 16GB 且非系统管理**，提交上限 = 15.7GB 物理
+ 16GB 页面文件 ≈ 31.7GB。模拟器一跑就顶到 31.5GB，Android 的
system_server 被饿死，弹「Process system isn't responding」。

缓解办法（按性价比排序）：

1. 构建前先 `adb emu kill` 停掉模拟器，别同时跑（Gradle 会报
   `Native memory allocation (malloc) failed`）
2. 测试时让 PC 服务端走**云端转写**：`STT_TRANSCRIBER=minimax` 启动，
   服务端提交量从 2905MB 降到 673MB
3. 根治要**以管理员身份**把页面文件改成「系统管理」或调大上限，然后重启

## 排查

```bash
adb logcat -s MikuAgent:* MikuJS:* AssetServer:* RemoteClient:* ModelSync:* AudioCapture:*
```

关键行：

- `RENDER OK {...}` —— 渲染成功的自证信息（Core 版本、贴图数与尺寸、
  Drawable 数、参数数、包围盒、缩放）
- `RENDER_FAILED ...` —— 渲染失败原因
- `LOCAL_BOUNDS {...}` / `LAYOUT ...` —— 模型定位（排查「只看到一半」用）
- `补全 N 个表情 / M 个动作` —— model3.json 内存补全是否生效
- `模型就绪：下载 N 个，跳过 M 个` —— 模型同步
- `已连接 ws://...`
- `录音结束 X.XXs / N KB` → `上传语音 X.XXs`

PC 侧同一时刻的交互记录在 `data/remote.log`（对话 / 语音 / 转写）。

## 当前进度

**四项核心功能已全部在模拟器（Android 15）上验证通过**：

| 功能 | 证据 |
|---|---|
| 模型渲染 | `RENDER OK textures=6 texSizes=[4096x4096 ×3] parts=77 params=141 drawables=440`；截图确认全身居中 |
| 文字对话 + 情绪 | 气泡「晚上好呀主人～ミクです！☆…」`chip=开心` |
| 语音上传转写 | `语音转写 1.56s`（模拟器无真实麦克风，采到静音属预期） |
| 拍照 + 视觉对话 | `onCaptureSuccess 1920x1440` → `对话 3.29s (带图=True)`，Miku 回复「你是自己画的吗？还是从哪个游戏里截的呀」 |

`data/remote.log` 里「对话 / 语音 / 转写」记录从 **0 条**变为有记录。

**Android Studio 已就位**：`D:\Android\android-studio`（免安装版，
自带 JDK 25，桌面已建快捷方式）。项目 `local.properties` 已指向
`D:\Android\Sdk`，打开本项目就会用对 SDK。

未完成：

- **真机验收**：这是唯一还差的验收项。需要你在 iQOO 上开 USB 调试并插线。
  重点看两件事：① 384MB 贴图（6×4096²）在真机 GPU 上的实际表现 ——
  模拟器是用 SwiftShader 绕开了 Chromium 的 GPU 黑名单，显存表现**不代表真机**；
  ② 真实语音识别效果。
- **模型表情切换的实机观感**：9 个表情已在内存里补进 model3.json 并默认应用了
  「水印」，但情绪→表情的映射（`EMOTION_EXPR`）只做过静态验证。
