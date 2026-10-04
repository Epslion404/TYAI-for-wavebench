# 配置 Reference

WaveBench 从指定路径或默认的 `wavebench.toml` 加载本地实验台配置。该文件缺失时，依赖配置的命令会报错；`wavebench.example.toml` 是示例，不是完整 schema。

## 必需表与可选表

| 类别 | 表 |
| --- | --- |
| 必需 | `[connection]`、`[scope]` |
| 可选 | `[autoscale]`、`[waveform]`、`[output]`、`[quality]`、`[safety_limits]`、`[tui]`、`[source]`、`[rf_source]`、`[power]`、`[dmm]`、`[advisor]` |

字段、默认值和跨字段约束由 config model 与 parser 定义。修改 plan 或配置前，先核对[示例配置](https://github.com/Scaxlibur/wavebench/blob/master/wavebench.example.toml)、当前 CLI help 和相关 Reference。

## 最小结构

```toml
[connection]
backend = "lan"
resource = "TCPIP::192.0.2.10::INSTR"

[scope]
driver = "rtm2032"
access = "read_only"
```

示例中的 IP 地址是文档保留地址。真实资源、序列号、凭据和本地实验产物不得提交到公开仓库。

## 串口连接

`connection.backend = "serial"` 时，`[connection]` 直接持有串口参数；它们同时适用于 scope 与已配置的 source／power 驱动，每台仪器仍各用自己的 `resource`。只有选中的 descriptor 声明了 `serial` backend 时该值才可用，否则会在打开传输前被拒绝。DMM 保留独立的 `dmm.*` 串口字段（历史路径）。

| 字段 | 默认值 | 约束 |
| --- | --- | --- |
| `resource` | 必填 | 串口设备路径，例如 `COM7`、`/dev/ttyUSB0`、`ASRL/dev/ttyUSB0::INSTR`；不接受 `TCPIP::`／`USB::`／`GPIB::` 等 LAN／USB／GPIB 形式 |
| `baudrate` | `9600` | 必须大于 0 |
| `bytesize` | `8` | 只能是 7 或 8 |
| `parity` | `"N"` | `N`／`O`／`E`（也接受 `NONE`／`ODD`／`EVEN`） |
| `stopbits` | `1` | 只能是 1、1.5 或 2 |
| `write_termination` | `"lf"` | `lf` 或 `crlf` |
| `read_termination` | `"lf"` | `lf` 或 `crlf` |
| `xonxoff` | `false` | 软件流控 |
| `rtscts` | `false` | 硬件流控 |
| `dsrdtr` | `false` | 硬件流控 |

字段、默认值与校验以 config model 为准；本表不替代 parser 的报错。

## Access policy

`scope`、`source`、`rf_source`、`power` 和 `dmm` 配置可以使用以下 `access` 值：

| 值 | 行为 |
| --- | --- |
| `read_write` | 允许 descriptor 和 operation policy 已授权的读取与写入。 |
| `read_only` | 仅允许 observe 或 stateful read；写入和采集会在 I/O 前被拒绝。 |
| `disabled` | 拒绝仪器操作；离线命令仍可运行。 |

配置中的 `access` 不能替代真实接线、操作系统权限或仪器自身保护。

## Advisor 外发边界

`[advisor]` 控制 advisor（外部判断模型）能否把状态发出本机。默认 `enabled = false`，关闭时不会构造任何外发请求；开启必须同时给出 `endpoint_hosts` 与 `allowed_state_fields` 白名单，否则配置校验直接失败。`accept`／`review` 是「概率 → 动作」的阈值，由 Core 拥有，必须满足 `0 <= review <= accept <= 1`。

外发还需通过同意门：发送前产出完整预览（内容、字节数、sha256、逐条不可信来源），预览不联网；同意默认按次，可登记为同一 run 内有效，并绑定 endpoint 集合与允许字段集，任一变化即失效；非交互场景（run plan、CI、MCP）一律拒绝。结论写入对应 run 目录的 `decisions/`（schema `wavebench.decision.v1`），附加式，不影响 `run.json`、质量门或 `auto_recover`。

开发线入口是 `python -m wavebench advisor ask --state <fixture> --config wavebench.toml [--preview | --accept-external-state]`；它只读取本表，不要求 `[connection]` 或 `[scope]`。实现状态与分期见 [advisor 插件类别 RFC](../project/rfcs/WaveBench_advisor插件RFC.md)（`Draft`），实现流程见[advisor 插件开发](../development/advisor-plugin-development.md)。

## 安全限制

`[safety_limits]` 用于在打开 transport 前限制 Source／Power 写入。Source V2 的端口电压下界和上界必须成对出现，并且下界小于上界。限制不应为了通过一次实验而放宽；应先确认实际端接、量程和实验要求。

## 相关页面

- [配置实验台](../getting-started/configure-bench.md)
- [CLI Reference](cli.md)
- [run plan Reference](run-schema.md)
- [仪器插件仓库](https://github.com/Scaxlibur/wavebench-instrument-plugins)
