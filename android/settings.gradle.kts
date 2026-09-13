// 仓库镜像必须显式配置，否则构建会挂死。
//
// 实测（见 docs/ANDROID_PLAN.md §2.3）：
//   repo1.maven.org   ❌ 不通   —— Maven Central 拉不到
//   maven.google.com  ❌ 不通
//   dl.google.com     ✅ 通     —— AGP / androidx 实际来源
//   maven.aliyun.com  ✅ 通     —— 阿里云的 google / public / gradle-plugin 三个仓库
//
// 所以把阿里云镜像排在前面，google()/mavenCentral() 只作兜底。
pluginManagement {
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/gradle-plugin")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        maven("https://maven.aliyun.com/repository/google")
        maven("https://maven.aliyun.com/repository/public")
        google()
        mavenCentral()
    }
}

rootProject.name = "MikuAgent"
include(":app")
