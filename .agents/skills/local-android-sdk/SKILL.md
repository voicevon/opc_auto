---
name: local-android-sdk
description: 本地计算机（Windows）Android 开发环境全景指南与配置速查手册。涵盖本机特定 JDK(JBR)、Android SDK、Gradle 缓存路径、版本兼容搭配及命令行执行规范。在搭建、构建、调试 Android 子应用时激活使用。
---

# 本机 Android 开发环境全景指南 (local-android-sdk)

本指南记录了当前 Windows 宿主机上已就绪的 Android 开发环境、工具链物理路径及构建执行规范。后续创建或维护任何 Android 项目时，直接依照本规范执行，无需重新探测环境。

---

## 1. 核心工具链与物理路径

### 1.1 JDK (Java 运行环境)
本机未向全局系统 `PATH` 注册 `javac`/`java`，统一使用 Android Studio 自带的 **JetBrains Runtime (JBR 21)**：
- **JAVA_HOME 路径**：`C:\Program Files\Android\Android Studio\jbr`
- **可执行文件**：`C:\Program Files\Android\Android Studio\jbr\bin\java.exe`
- **版本**：Java 21.0.8 (JetBrains s.r.o. 64-Bit Server VM)
- **命令前置约束**：在 PowerShell 中执行任何 Gradle 或 Java 命令前，**必须设置环境变量**：
  ```powershell
  $env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
  ```

### 1.2 Android SDK
- **SDK 根路径**：`C:\Users\feng\AppData\Local\Android\Sdk`
- **已安装 Platforms**：
  - `android-33`
  - `android-34`
  - `android-35` *(推荐 compileSdk / targetSdk)*
  - `android-36`
- **已安装 Build-Tools**：
  - `33.0.1` / `35.0.0` *(稳定推荐)* / `36.0.0` / `36.1.0`
- **ADB 工具**：
  - `C:\Users\feng\AppData\Local\Android\Sdk\platform-tools\adb.exe`
  - 系统已注册 PATH，可直接执行 `adb devices`、`adb install <path>`。

### 1.3 本地已缓存 Gradle
本机 Gradle 缓存中已有可用的免下载可执行脚本：
- **Gradle 8.11.1** *(当前主力推荐)*：
  `C:\Users\feng\.gradle\wrapper\dists\gradle-8.11.1-bin\bpt9gzteqjrbo1mjrsomdt32c\gradle-8.11.1\bin\gradle.bat`
- **Gradle 9.1.0**：
  `C:\Users\feng\.gradle\wrapper\dists\gradle-9.1.0-bin\9agqghryom9wkf8r80qlhnts3\gradle-9.1.0\bin\gradle.bat`

---

## 2. 验证通过的最佳版本搭配矩阵 (SSOT)

在当前机器上验证通过、构建零报错的组件版本组合如下：

| 组件 / 插件 | 验证版本 | 说明 |
| :--- | :--- | :--- |
| **Gradle** | `8.11.1` | 通过 Wrapper 运行 |
| **Android Gradle Plugin (AGP)** | `8.9.2` | 已在本地缓存，秒级解析 |
| **Kotlin** | `2.1.20` | 已在本地缓存 |
| **Kotlin Compose Compiler** | `2.1.20` | `org.jetbrains.kotlin.plugin.compose` |
| **KSP (Kotlin Symbol Processing)** | `2.1.20-1.0.31` | 与 Kotlin 2.1.20 完全对齐（Room 编译必须） |
| **Java 编译目标** | `JavaVersion.VERSION_17` | `jvmTarget = "17"` |
| **Android API 级别** | `compileSdk = 35`, `minSdk = 24`, `targetSdk = 35` | 覆盖现代 Android |
| **Jetpack Room** | `2.6.1` | `androidx.room:room-runtime:2.6.1` |
| **Compose BOM** | `2024.11.00` | 官方物料清单 |

---

## 3. 标准操作手册 (Runbook)

### 3.1 为新子应用初始化 Gradle Wrapper
若在根目录下新建子项目（例如 `clock/`），创建 `settings.gradle.kts` 后直接调用本机 Gradle 生成 Wrapper：
```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
& "C:\Users\feng\.gradle\wrapper\dists\gradle-8.11.1-bin\bpt9gzteqjrbo1mjrsomdt32c\gradle-8.11.1\bin\gradle.bat" wrapper --gradle-version 8.11.1
```

### 3.2 编译与产物验证命令
在子项目根目录下编译 Debug APK：
```powershell
$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"
.\gradlew assembleDebug
```
- 输出产物位置：`<app-dir>/app/build/outputs/apk/debug/app-debug.apk`

### 3.3 本机真机 / 模拟器调试安装
```powershell
adb devices
adb install -r <app-dir>/app/build/outputs/apk/debug/app-debug.apk
```

---

## 4. 常见问题与避坑指南 (Gotchas)

1. **`java : 无法将“java”项识别为 cmdlet...`**：
   - 必须通过 `$env:JAVA_HOME = "C:\Program Files\Android\Android Studio\jbr"` 注入，不要试图要求用户全局配置 PATH。
2. **`gradle wrapper` 报错 `Directory does not contain a Gradle build`**：
   - 执行 wrapper 前必须确保当前目录至少存在 `settings.gradle.kts`。
3. **KSP 与 Kotlin 版本不匹配**：
   - 若 Kotlin 升级，KSP 必须严格匹配前缀（例如 Kotlin 为 `2.1.20` 时，KSP 必须是 `2.1.20-1.0.xx`）。
4. **Jetpack Compose 排版参数混淆**：
   - `Column` 的横向对齐为 `horizontalAlignment = Alignment.CenterHorizontally`（不是 `CenterVertically`）。
   - `Row` 的纵向对齐为 `verticalAlignment = Alignment.CenterVertically`（不是 `CenterHorizontally`）。
