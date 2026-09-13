// 顶层构建脚本：只声明插件版本，不在这里 apply。
// 版本组合已在阿里云镜像上逐个校验过（HTTP 200）：
//   AGP 8.6.1 / Kotlin 2.0.21 / Gradle 8.9 / JDK 21
plugins {
    id("com.android.application") version "8.6.1" apply false
    id("org.jetbrains.kotlin.android") version "2.0.21" apply false
}
