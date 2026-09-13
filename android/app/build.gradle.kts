// 手机端（路线 A）Android 客户端。
//
// 设计要点（详见 docs/ANDROID_PLAN.md）：
//   * WebView **只负责 Live2D 渲染**，且不碰网络 —— 页面来自 WebViewAssetLoader 的
//     本地 https 源，模型由 AssetServer 拦截后喂入。这样浏览器那套
//     「安全上下文 / mixed-content」限制就完全不存在了。
//   * WebSocket、录音、播放、口型包络、相机全部走 Kotlin 原生实现。
//   * **模型不打进 APK**：模型授权写明「不可二传二改」，改为运行时从 PC 拉取
//     到应用私有目录缓存。
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "com.mikuagent.pet"
    compileSdk = 35

    defaultConfig {
        applicationId = "com.mikuagent.pet"
        minSdk = 26          // AudioRecord / WebViewAssetLoader / CameraX 都够用
        targetSdk = 35
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(
                getDefaultProguardFile("proguard-android-optimize.txt"),
                "proguard-rules.pro",
            )
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }
    kotlinOptions {
        jvmTarget = "17"
    }

    packaging {
        resources.excludes += setOf("META-INF/*.kotlin_module")
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.webkit:webkit:1.12.1")   // WebViewAssetLoader

    implementation("com.squareup.okhttp3:okhttp:4.12.0")   // WebSocket + 模型下载

    // 相机：拍照发给 Miku 看
    implementation("androidx.camera:camera-core:1.3.4")
    implementation("androidx.camera:camera-camera2:1.3.4")
    implementation("androidx.camera:camera-lifecycle:1.3.4")

    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")
}
