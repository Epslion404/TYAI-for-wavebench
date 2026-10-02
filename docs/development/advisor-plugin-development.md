# 开发 advisor 插件

advisor 插件为 WaveBench 提供建议，不接管仪器控制。它们必须使用 `wavebench.advisor` entry point，返回
`AdvisorPlugin` metadata，并声明 `wavebench.advisor.v1` API 版本。

## 不可跨越的边界

- 建议不得改变输出、采集、供电、`run.json.status`、质量门、`auto_recover` 或 capability/access policy。
- 命令、SCPI 与可执行动作必须由 Core 的封闭目录提供；插件不得把模型输出直接变成命令。
- 默认路径必须离线可运行。无 client、无 key、调用失败或响应异常时，返回无建议或 `unavailable`，不能抛出后继续执行。

## 外发声明

不向机器外发送数据的插件使用 `EgressDeclaration(transmits_off_machine=False, purpose=...)`，并且不能声明
endpoint 或 `advisor.external_state` capability。向外发送状态的插件必须：

1. 仅声明实际使用的 `endpoint_hosts` 与 `allowed_state_fields`；
2. 声明 `advisor.external_state` capability；
3. 在每次调用前让 Core 构造脱敏预览并校验 `[advisor]` 中的两份白名单；
4. 只在用户明确确认后调用，endpoint、字段集或 run 改变时重新确认；
5. 把结果作为附加式 decision artifact 写入对应 run 的 `decisions/`，不修改既有 run artifact。

不要把 API key、原始波形、不可信仪器响应正文或未登记字段放进请求。开发和测试都应使用 fake client，
验证 preview、未同意、白名单不符与调用失败时外部调用次数为零。

## 验证

```bash
python -m wavebench plugin list --category advisor
python -m wavebench plugin doctor --category advisor
python -m pytest -q tests/test_advisor_plugins.py tests/test_advisor_consent.py
```

entry point 导入必须不做网络访问、仪器 I/O 或文件写入。完整字段与 validator 以
`src/wavebench/plugins/api.py` 和 `src/wavebench/plugins/registry.py` 为准。
