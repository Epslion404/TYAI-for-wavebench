# 插件 Reference

WaveBench 支持下列彼此独立的插件路径。它们的共同边界是：Core 拥有 resource、transport、Service、安全、run plan、artifact 和 session/recovery；插件实现厂商协议、命令、解析、descriptor 和私有测试。

| 路径 | 入口 | 是否进入真实执行 |
| --- | --- | --- |
| V1 metadata | `wavebench.drivers` / `wavebench.instrument.v1` | 否。用于展示 metadata。 |
| V2 executable plugin | `wavebench.instruments` / `wavebench.instrument.v2` | 仅在配置选中 driver 后。 |
| 声明式 SCPI TOML | 本地 TOML | 否；只允许显式只读 IDN probe。 |
| 声明式二进制帧 TOML | `plugin protocol check/info`、`DeclarativeProtocolRuntime` | 否；CLI 仅离线校验，运行时 API 需调用方提供 transport。 |
| advisor 类别 | `wavebench.advisor` / `wavebench.advisor.v1` | 否；只产出建议，不连接仪器。 |

## 加载与信任边界

默认插件命令优先查看内建 metadata。`plugin ... --load` 会导入第三方 V2 descriptor；只在可信环境中使用。Service 仅在实际打开已配置的 driver 时加载对应插件，未选中的坏插件不应阻断其它内建路径。

V2 entry point 名必须与 canonical `driver_id` 一致。可执行插件不能通过 alias 覆盖内建 driver；具体分发包、型号支持和 capability 以 descriptor 与插件仓库为准。

## 声明式 SCPI 与本地市场索引

`plugin scpi check`、`plugin scpi doctor` 和 `plugin scpi info` 默认只读取 TOML。只有显式 `--probe --resource` 或 `plugin scpi probe` 才会发送配置的单行 IDN 查询；该路径不提供任意 SCPI 或写入。

`plugin market` 读取本地 JSON 索引，用于搜索和查看条目。它不下载、安装、导入或执行索引中声明的包。

字段、默认值和 JSON／TOML 校验规则由 parser 与 validator 定义。不要把本页的概览当作完整 schema。

## 声明式二进制帧

本节描述开发线的离线校验切片，尚未随正式版本发布。完整协议接入提案的状态见
[RFC 索引](../../rfcs/index.md)。

`plugin protocol check` 和 `plugin protocol info` 通过
位置参数 `path` 读取本地 TOML，不打开 transport。字段与默认值由
`src/wavebench/plugins/protocol.py` 定义；manifest 上限为 1 MiB，帧与 header 上限为 64 MiB。
插件 metadata 和必填帧字段必须完整，非法描述统一返回配置错误。

支持定长、长度字段和分隔符三种 framing，以及可选 CRC16/Modbus。单帧校验要求完整
header 和精确长度；分隔符 framing 以首个未转义分隔符结束，拒绝内部未转义分隔符和
拼接帧。
`escape_hex` 可为分隔符 framing 声明一个单字节转义（escape 转义自身与分隔符字节），
未转义分隔符仍表示帧尾。帧校验返回原始字节。开发线还提供流拆帧
（`extract_stream_frame`，跨分包累积并校验完整帧）、请求映射
（`[[protocol.requests]]`：命名请求的固定帧与响应超时）与解码原语。
`DeclarativeProtocolRuntime` 是请求映射的执行入口：它用 `encode_request` 构造
请求帧，经 `send_frame` 以 `NO_REPLAY` 发出，再按声明的 framing 拆分响应，
并应用请求级/帧级响应超时与 `inter_frame_gap_ms`。未知请求、无法编码的请求帧
和非 `NO_REPLAY` 策略在 I/O 前拒绝；传输失败保持结构化 `TransportIOError`，
由 guarded session 按同步状态污染连接；响应拆帧失败同样污染 guarded session。
采样解码以及 CLI/驱动对运行时的接入仍由后续协议合同定义。

```python
from wavebench.plugins.protocol import load_protocol_plugin
from wavebench.plugins.protocol_runtime import DeclarativeProtocolRuntime

declaration = load_protocol_plugin("sensor.toml")
runtime = DeclarativeProtocolRuntime(declaration, guarded_transport)
response = runtime.request("read_status")
```

## advisor 类别

`advisor` 是第二类插件：接收应用状态与封闭问题集，输出 typed judgment。它不连接仪器，也不参与写路径、安全门、质量门与 capability 判定；数据外发同意门与 `wavebench.decision.v1` 产物由 Core 拥有，插件只声明 egress 事实。

`plugin list` 与 `plugin doctor` 用 `--category {instrument|advisor}` 选择类别（默认 `instrument`）。`--include-entry-points` 在 advisor 类别下加载 `wavebench.advisor` 入口点；`--kind` 与 `--load` 只对仪器插件有效，advisor 类别下会报错。

实现状态：开发线已实现，未随正式版本发布；包内安装、升级、降级、卸载与恢复已覆盖，postflight 走 `validate_advisor`。`plugin doctor --category advisor` 会从 `Requires-Dist` 报告缺失依赖，不联网。合同状态见 [RFC 索引](../../rfcs/index.md)，实现流程见[advisor 插件开发](../../development/advisor-plugin-development.md)。

## 相关页面

- [管理仪器插件](../../how-to/manage-plugins.md)
- [插件开发](../../development/plugin-development.md)
- [advisor 插件开发](../../development/advisor-plugin-development.md)
- [新增仪器驱动](../../development/instrument-drivers.md)
- [仪器插件仓库](https://github.com/Scaxlibur/wavebench-instrument-plugins)
