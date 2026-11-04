# 看见 Kanjian

> 看见 — 把摄像头里的环境变成**简短、可打断、带不确定性提示**的语音描述，帮助视障用户理解周围场景。

**Kanjian 是看见 / Tingjian 是听见**。如果你在找助听方向的姊妹项目，见 [tingjian](https://github.com/Anguis-L/tingjian)。

## ⚠️ 这不是什么（先读这个）

**Kanjian 不是导航或避障设备。它不告诉你"能不能走"，也不替代盲杖、导盲犬、定向行走训练或你自己的判断。** 它只描述摄像头里看到了什么，并明确哪些内容不确定。

完整安全边界见 [SAFETY.md](SAFETY.md)。

## 它做什么

| 场景 | 它说 | 它绝不说 |
|---|---|---|
| 环境理解 | "像是一条走廊，正前方有椅子，左侧有门。" | "向左绕过去" |
| 主动询问 | "看起来是垃圾桶，画面有些模糊。" | "两米外有垃圾桶" |
| 文字阅读 | 朗读识别出的文字；看不清的明确跳过 | 补写看不清的字 |

设计原则：**先少后准**——少播报、短句、可打断，优先于说得丰富。

## 隐私默认值

- **默认不保存任何图像**；图像只在内存中存活到请求结束
- 不上传连续视频；不做人脸识别或身份识别
- 无用户账户；历史只存文字描述，可一键全部清除
- 云端可彻底关闭；关闭后无任何图像外发（可抓包验证）
- 数据是否离端、默认保存什么，见 [PRIVACY.md](PRIVACY.md)

## 架构

```
摄像头 → 帧采样(0.5/1fps) → 事件触发 → 单飞推理 → 安全策略过滤 → 单通道语音
```

- **端侧拥有最终输出权**：任何模型输出（云端、本地、第三方）都过同一套策略层才能播报；提示词不是安全边界
- **Provider 可替换**：参考实现 = Fake（离线回放）+ 任意 OpenAI 兼容端点；本项目不内置任何默认端点——用自己的 key，自己的数据自己负责
- **预算保护不可关闭**：默认 300 次/日、¥3/日、100 次/会话；达 80% 提醒、100% 暂停自动模式；手动询问不受阻

技术栈：Python 3.12 / FastAPI（网关）· Kotlin/CameraX/Compose（Android，进行中）。

## 快速开始（网关原型）

```bash
git clone https://github.com/Anguis-L/kanjian.git
cd kanjian
python -m venv .venv && .venv\Scripts\activate   # Windows
pip install -e ".[dev]"
pytest                                            # 133 个测试
uvicorn gateway.app:app --port 8000               # Fake Provider，零网络
```

接你自己的 OpenAI 兼容端点（`base_url` 带不带 `/v1` 都可以）：

```bash
export OPENAI_BASE_URL="https://your-endpoint.example/v1"
export OPENAI_API_KEY="sk-..."
export KANJIAN_OPENAI_MODEL="your-vision-model"   # 可省略：会从端点 /v1/models 自动挑支持图像的模型
uvicorn gateway.app:app --port 8000
```

也可以写在仓库根的 `.env` 里（已被 `.gitignore` 忽略，不会进仓库），见 [.env.example](.env.example)。

API：`POST /v1/scene:describe`（multipart：metadata JSON + JPEG 图像）· `GET /v1/health` · `GET /v1/capabilities` · `GET /v1/usage`。契约 Schema 见 [schemas/](schemas/)。

### 验证真实链路

```bash
python scripts/smoke_provider.py          # 端到端：图 → 模型 → Schema → 策略 → 播报
python scripts/bench_latency.py --runs 5  # 分辨率/延迟基准
python scripts/live_injection_test.py     # 真实提示注入对抗
```

实测数字见 [docs/BENCHMARKS.md](docs/BENCHMARKS.md)。**当前参考模型延迟未达 P50≤2.0s 目标，偏差已如实记录。**

## 当前状态

原型阶段（M0→M1）。

**已完成**：schemas 契约与正反例、OutputPolicyEngine（对抗集放行数 0）、TriggerEngine（冷却/滞回/降级）、BudgetGuard、SceneFingerprint、网关与双 Provider。

**Android 端（M1 垂直切片，可编译可测）**：CameraX 预览与单帧拍摄 → 网关 → **端侧策略二次过滤** → 系统 TTS 播报；首次安全说明、大按钮与 TalkBack 语义、失败可感知。Kotlin 端与 Python 端共用同一组 37 条对抗样本。

APK 构建与 SDK 安装见 [docs/ANDROID.md](docs/ANDROID.md)。**未经分级验证（G0–G4），不宣传任何出行能力。**

## 许可证

| 资产 | 许可证 |
|---|---|
| 软件代码 | Apache-2.0（[LICENSE](LICENSE)） |
| 文档 | CC BY-SA 4.0（[docs/LICENSE](docs/LICENSE)） |

第三方组件见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。名称 Kanjian/看见 不因代码开源而授权商标使用（[TRADEMARK.md](TRADEMARK.md)）。

## 更多

[SAFETY.md](SAFETY.md) · [PRIVACY.md](PRIVACY.md) · [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · 设计文档见 [docs/](docs/)
