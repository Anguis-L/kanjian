# Android 端构建

## 本机已验证的工具链

| 组件 | 版本 | 位置 |
|---|---|---|
| JDK | Temurin 21.0.12 | `C:\jdk-21`（`JAVA_HOME`） |
| Android SDK | cmdline-tools 12.0 | `H:\Android\Sdk`（`ANDROID_HOME`） |
| platform | `android-34` | |
| build-tools | `34.0.0` | |
| Gradle | 8.7（wrapper） | |
| AGP | 8.5.2 | |
| Kotlin | 1.9.24 | |

产物：`android/app/build/outputs/apk/debug/app-debug.apk`（约 4.0 MiB）
验证：`minSdk 26` / `targetSdk 34` / `compileSdk 34`，包名 `org.kanjian.app`。

### 从零安装 SDK

```powershell
# 1) JDK 21（AGP 8.5 要求 JDK 17+）
# 2) cmdline-tools
Invoke-WebRequest "https://dl.google.com/android/repository/commandlinetools-win-11076708_latest.zip" -OutFile ct.zip
Expand-Archive ct.zip -DestinationPath "$env:ANDROID_HOME\cmdline-tools"
Move-Item "$env:ANDROID_HOME\cmdline-tools\cmdline-tools" "$env:ANDROID_HOME\cmdline-tools\latest"

# 3) 许可 + 组件
$sm = "$env:ANDROID_HOME\cmdline-tools\latest\bin\sdkmanager.bat"
("y`n" * 50) | & $sm --licenses
& $sm "platform-tools" "platforms;android-34" "build-tools;34.0.0"
```

> `local.properties` 里的 `sdk.dir` 是本机绝对路径，已在 `.gitignore` 中，不会进仓库。
> CI 与用户机器通过 `ANDROID_HOME` 找到 SDK，不需要该文件。

## 构建

```bash
cd android
./gradlew :app:assembleDebug        # 打 APK
./gradlew :app:testDebugUnitTest    # Kotlin 单测（策略一致性）
```

Windows 用 `gradlew.bat`。

## 连接网关

App **不内置任何默认端点**（ADR-07）。有两种配置方式：

### 方式一：App 内设置（侧载安装后使用）

点「设置服务地址」，填入你自己的网关地址（含 `http://` 或 `https://`），确定后立即生效，配置保存在本地。

手机与电脑需在同一网络，填**电脑的局域网 IP**，例如 `http://192.168.1.10:8000`。
网关必须监听 `0.0.0.0` 而不是 `127.0.0.1`：

```bash
uvicorn gateway.app:app --host 0.0.0.0 --port 8000
```

未配置时，点「询问前方」会直接弹出设置对话框，不会尝试任何默认地址。

**不在同一 Wi-Fi 时**：如果两端都在同一个 Tailscale（或同类 overlay 网络）里，
直接填电脑的 Tailscale IP + 网关端口即可，不必同网段：

```
http://<电脑的 Tailscale IP>:8000
```

注意填的是**网关端口（默认 8000）**，不是推理服务的端口——
App 走的是本项目的 `/v1/scene:describe` 协议，不是 OpenAI 的 `/v1/chat/completions`。

> 若你的推理服务（如 magpie）把 API key 限制为仅本机回环可用，
> 网关应连接 `127.0.0.1:<port>`，不要把网关的上游指到外层网卡地址。

### 方式二：构建时注入（开发调试）

```bash
./gradlew :app:assembleDebug -Pkanjian.gatewayUrl=http://192.168.1.10:8000
```

App 内设置优先于构建时注入的值。

> 原型期 Manifest 开了 `usesCleartextTraffic="true"` 以便连局域网自建网关。
> **公开发行前必须改为 HTTPS 并移除该属性。**

## 当前实现范围（M1 垂直切片）

已实现：

- CameraX 预览 + 单帧拍摄（`ImageCapture`，长边 ≤1280、JPEG q80）
- 主动询问：拍一帧 → POST `/v1/scene:describe` → 解析 SceneResult
- **端侧 OutputPolicy 二次过滤**（ADR-02：端侧是最终防线）
- 系统 TTS 单通道播报、立即停止、重复播放
- 首次启动安全/隐私说明（FR-ONB-001）
- 大按钮（minHeight 72dp）、`contentDescription`、`accessibilityLiveRegion`
- 失败可感知：超时/离线/服务不可用分别给出不同提示

**未实现**（对应 C 级缺口）：

- 自动观察（需 TriggerEngine + FrameSampler，M2）
- OCR 模式与框选重拍
- 本地历史与一键清除
- 预算 UI 与 BudgetGuard 接入
- TalkBack 全流程实测（需真机或模拟器）
- 音频焦点策略（ADR-11 的 MAY_DUCK）

## 策略一致性

`app/src/test/java/org/kanjian/app/OutputPolicyTest.kt` 使用与 Python 版
`tests/test_policy.py` **同一组 37 条对抗样本**。两端必须同时拦住同一批输入，
否则"端侧是最终防线"就是空话——用户装的是 App，不是 Python 服务。

开发中这条测试抓到一个真实问题：Kotlin 移植时漏了英文敏感属性规则，
`This person looks angry.` 在 Python 端被拦、在 App 端放行。**这就是它存在的意义。**

## 已知环境问题

- `gradle wrapper` 任务会 HEAD 校验发行包（10s 超时）。若网络到
  `services.gradle.org` 较慢会失败，可先用本地 zip 生成再改回官方 URL。
  当前 `gradle-wrapper.properties` 已设为 `networkTimeout=60000`。
- **MuMu 模拟器（Android 12 / API 32 / x86_64）的已知限制**：
  - 没有装任何 TTS 引擎（`tts_default_synth=null`）→ 只能验证"语音不可用"分支，
    无法验证真实播报。App 会显示"〔语音不可用，仅屏幕显示〕"。
  - 默认摄像头输出全黑（`EmulatedCamera3_QemuSensor` 有帧，但画面是黑的）。
    模型会如实回答"画面为空白，未见到可辨认的内容"——**不幻觉**，这是期望行为；
    但在 MuMu 上无法验证"真实场景描述"。
  - 摄像头来源需在 MuMu 图形界面里设置，无配置文件可脚本化。

用真机测才能覆盖：真实场景描述、TTS 播报、TalkBack 遍历。

## 已在本机验证（MuMu）

| 项 | 结果 |
|---|---|
| 安装与启动 | ✅ 无崩溃，冷启动 1.8s |
| 首次安全说明弹窗 | ✅ 文案完整、可点"我已了解" |
| 相机权限 | ✅ 自动授予，CameraX 绑定成功并持续出帧 |
| **完整链路** | ✅ 点击 → 拍摄(8.0 KB JPEG) → `adb reverse` → 网关 → 真实模型 → 解析 → 端侧策略 → 屏幕显示 |
| request_id 校验 | ✅ 网关回传客户端 id，App 校验一致 |
| 失败路径 | ✅ 超时时显示"识别超时，可重试" |
| 语音不可用 | ✅ 显示"〔语音不可用，仅屏幕显示〕" |
