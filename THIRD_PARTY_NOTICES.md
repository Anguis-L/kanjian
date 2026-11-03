# 第三方组件清单（THIRD_PARTY_NOTICES）

> 文件级记录，不以仓库标签代替审查。**每个改变依赖的 PR 必须同步更新本文件**（00 §5.1）。

## 运行时依赖

| 包 | 许可证 | 用途 | 备注 |
|---|---|---|---|
| fastapi | MIT | HTTP API 框架 | |
| uvicorn | BSD-3-Clause | ASGI 服务器 | `[standard]` 附带 httptools/uvloop/websockets 等，均为宽松许可 |
| pydantic | MIT | 数据校验 | |
| jsonschema | MIT | Schema 校验 | 两端契约测试共用 |
| httpx | BSD-3-Clause | HTTP 客户端 | 调用外部 Provider |
| python-multipart | Apache-2.0 | multipart 解析 | |

## 开发依赖

| 包 | 许可证 | 用途 |
|---|---|---|
| pytest | MIT | 测试框架 |
| pytest-asyncio | Apache-2.0 | 异步测试 |
| ruff | MIT | 静态检查（可选） |

## 与本项目交互但不随本项目分发的组件

| 组件 | 性质 | 责任边界 |
|---|---|---|
| 用户自配的 OpenAI 兼容推理服务 | 用户自行选择与付费 | 模型权重许可证、服务条款、数据处理均由用户与该服务方约定；本项目不打包、不背书 |
| 自托管推理框架（如 vLLM） | 可选部署路径，非交付物 | 同上 |
| Android 系统 TTS / TalkBack | 系统组件 | 由设备系统提供 |

## 未包含的内容

- **不包含任何模型权重。** 本项目不分发 VLM/TTS 权重；用户配置的外部端点自行负责其许可证
- 不包含评测数据集。经授权的评测数据单独存放，授权信息见 `docs/DATA_CARD.md`

## 明确禁止

- **禁止混入权重为 CC-BY-NC 等仅限非商业用途的组件**，除非整个项目明确接受其全部传播限制（00 §5.5）
- 不因"可下载"推定"可商用"或"可再分发"

## 审查方法

版本在首次构建时锁定并写入依赖清单；许可证通过 CI 扫描核对。发现清单与实际情况不一致时，以实际为准并立即修正本文件。
