# 贡献指南（CONTRIBUTING）

感谢你想帮这个项目。请先读 [SAFETY.md](SAFETY.md)——本项目的贡献门槛由安全边界决定，而不是由功能数量决定。

## 最重要的三条

1. **不要提交会削弱输出安全策略的代码。** 任何放宽策略的改动都需要附对抗样本测试证明没有放开漏洞。
2. **不要引入来源不明的数据、模型权重、图像或语音。** 一律不合并。
3. **不要提交真实用户的图像、语音或可识别信息。** 测试数据必须是合成的或已授权的。

## 开发流程

```bash
git clone https://github.com/Anguis-L/kanjian.git
cd kanjian
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

提交前请确保：

- `pytest` 全绿
- 涉及策略的改动必须补充对应的对抗样本
- 涉及接口的改动必须同步更新 `schemas/` 与两端契约测试（Schema 是唯一来源，禁止两端各写一份）
- 新增依赖必须同时更新 `THIRD_PARTY_NOTICES.md`

## 提交信息

采用简洁的祈使句，说明"做了什么"，例如：

```
policy: 拦截"可以，走"式标点切断绕过
gateway: 补齐 PROVIDER_TIMEOUT 到 504 的映射
```

## PR 检查清单

- [ ] 没有让任何 Provider 输出绕过端侧策略的路径
- [ ] 没有新增无依据的距离/方位/安全结论表述
- [ ] 新需求带了对应测试；旧测试没有被无理由删除
- [ ] 没有把图像/音频/base64 写进日志或测试夹具
- [ ] 依赖与许可证清单已更新
- [ ] 没有在本 PR 中悄悄改变已有行为的语义

## 代码与文档的许可证

- 代码贡献按 Apache-2.0
- 文档贡献按 CC BY-SA 4.0

提交即表示你同意以上许可，并声明你有权提交该内容。

## 关于 CLA

项目早期不要求签署 CLA。**如果你计划贡献可能进入未来独立授权模块的核心代码，请先在 issue 中讨论**——避免后期授权分层时出现权利不可处理的情况。

## 不要做的事

- 不要在本项目基础上宣传"可独立出行"或"安全避障"
- 不要把 Kanjian 包装成医疗器械
- 不要在未经授权的情况下使用 Kanjian 名称与标识做商业推广（见 [TRADEMARK.md](TRADEMARK.md)）
