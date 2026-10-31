# 命令与输出契约

读取时机：需要确认命令参数、JSON 字段、退出码或离线行为时读本页。普通测量只需要
[SKILL.md](../SKILL.md) 的最小工作流，不必先加载本页。

## 入口与安装

在本 Skill 目录执行：

```powershell
python -m venv .venv
.venv\Scripts\python -m pip install -e .
```

安装后可继续使用 `python -m tianyi_electronics_skill <command>`，或使用
`tianyi-electronics <command>`。真实 VISA 设备可额外安装 `.[visa]`；离线分析和演示不需要。

所有设备命令把结果写到 stdout，格式为 UTF-8 JSON。未捕获的异常写到 stderr：

```json
{"ok": false, "error": "..."}
```

## 通用约定

- 面向设备的命令尽量在 `failures` 中记录 `{category, command, message}`；`device_errors`
  保存设备错误队列内容。不要只根据 `ok` 判断设备状态。
- 本地/离线命令（`advise`、`analyze --demo`、`plot`、`protocol identify/decode`）也会返回
  `device_errors`，但不会伪造设备错误队列。
- 退出码：普通命令成功为 `0`，失败为 `2`；`autoscale` 的 `3` 表示只验证应用了部分设置，
  详见下文。
- `discover` 的 `--subnet` 是必填项；不要用空网段猜设备。

## discover

扫描网段并对常见端口尝试 `*IDN?`：

```powershell
python -m tianyi_electronics_skill discover --subnet 192.168.1.0/24
```

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--subnet` | 必填 | CIDR 网段 |
| `--ports` | `5025,5555,4000,502` | 逗号分隔端口 |
| `--timeout` | `0.35` | 单次探测超时（秒） |
| `--workers` | `64` | 并发探测数 |
| `--force` | 关闭 | 允许超过默认 4096 个 IPv4 目标；IPv6 仍受上限保护 |
| `--probe-non-scpi` | 关闭 | 对非 SCPI 端口也发送 `*IDN?` |
| `--mdns` | 关闭 | 额外查询 `_scpi._tcp.local` |

输出 `devices` 数组；每项至少包含 `address`、`port`、`protocol`、`resource`、`status`，
必要时带 `idn`、`source`、`note`。`device_errors_status` 在发现阶段为
`not_queried_during_discovery`，表示没有逐台读取错误队列。

## inspect

只读检查身份、`*OPC?`、错误队列和 CHAN1–CHAN4 显示状态：

```powershell
python -m tianyi_electronics_skill inspect --address 192.168.1.20 --port 5025
```

参数：`--address`（必填）、`--port`（默认 `5025`）、`--timeout`（默认 `2.0`）。

输出包含 `address`、`transport`、`timestamp`、`idn`、`opc`、`vendor`、`channels`、
`device_errors`、`transport_errors`、`failures`。某通道不支持或返回空响应时，
`channels.<CHAN>.display` 为 `null`，原因放在 `unsupported`，不要把它当成 0 V。

## measure

读取常见示波器测量值：

```powershell
python -m tianyi_electronics_skill measure --address 192.168.1.20 --channel CHAN1
```

参数：`--address`、`--port`、`--channel`（默认 `CHAN1`）、`--timeout`（默认 `2.0`）、
`--no-fetch-fallback`。默认在全部标量测量都不支持时降级为“读波形 + 离线分析”；
加 `--no-fetch-fallback` 可关闭降级。

输出字段：

- `measurements`：已由错误队列验证的 `frequency_hz`、`vpp_v`、`vmax_v`、`vmin_v`、
  `vavg_v`、`vrms_v` 子集；
- `commands`：每个成功指标实际使用的 SCPI 命令；
- `unsupported`：未取得指标及每条方言尝试的失败原因；
- `fetch_fallback`：降级读取的 `ok`、`metrics`、`sample_count`、`warnings`、`errors`；
- `errors`、`failures`、`device_errors`。

## autoscale

先读取建议，只有显式 `--apply` 才写设备：

```powershell
python -m tianyi_electronics_skill autoscale --address 192.168.1.20 --channel CHAN1
python -m tianyi_electronics_skill autoscale --address 192.168.1.20 --channel CHAN1 --apply
```

参数：`--address`、`--port`、`--channel`、`--timeout`、`--apply`。不加 `--apply` 时只探测和
计算建议，不改变设备状态。

退出码：

| 退出码 | 含义 |
| --- | --- |
| `0` | 只读建议完成；或 `--apply` 的全部设置都已验证应用且清理成功 |
| `2` | 执行失败；或 `--apply` 没有验证任何设置；或全部验证后关闭连接失败 |
| `3` | `--apply` 只验证应用了部分设置，需要人工决定恢复或重试 |

`applied` 与 `partially_applied` 按已验证设置统计。全部设置已验证但关闭失败时，保留
`applied=true`、`partially_applied=false`，退出码为 `2`。失败条目的 `command` 是实际执行的
SCPI 命令；连接或清理阶段明确记录 `connect`、`configure_session` 或 `close`。

## fetch

读取原始波形，输出时间轴、一致性校验和离线指标：

```powershell
python -m tianyi_electronics_skill fetch --address 192.168.1.20 --channel CHAN1 --output wave.csv
```

参数：

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| `--address` | 必填 | 设备地址 |
| `--port` | `5025` | SCPI 端口 |
| `--channel` | `CHAN1` | 通道 |
| `--timeout` | `5.0` | 总超时（秒） |
| `--points` | `auto` | 采样点数；`auto` 不写 `DATA:POINTS`，但仍设置传输格式 |
| `--format` | `real` | `real` 或 `ascii` |
| `--sample-rate` | 由 header 推导 | header 不可信时显式指定 |
| `--output` | 不写文件 | 以 `.csv` 或 `.json` 结尾；CSV 列为 `time_s,value_v` |
| `--plot-output` | 不生成 | 额外写 SVG 单帧图 |
| `--include-samples` | 关闭 | 把完整样本内联到 stdout |

默认 stdout 不包含完整 `samples`，只给摘要；需要传递大数组时用 `--output`。读取失败不伪造数据：
`sample_count` 保持 `0`，原因写入 `errors`/`failures`。

## analyze 与 plot

离线分析 CSV/JSON：

```powershell
python -m tianyi_electronics_skill analyze --file wave.csv
python -m tianyi_electronics_skill analyze --demo
```

`--file` 省略或 `--demo` 时使用内置演示波形。`--sample-rate` 默认 `100000`，仅在文件没有可用
时间轴时使用。CSV 支持 `time_s,value_v` 两列或单列值；JSON 读取 `samples` 和
`sample_rate_hz`。指标含义见 [waveforms.md](waveforms.md)。

生成不依赖第三方绘图库的 SVG：

```powershell
python -m tianyi_electronics_skill plot --file wave.csv --output frame.svg --title "CHAN1"
```

`--output` 默认 `waveform.svg`；`--sample-rate` 默认 `100000`。

## advise

离线规则基线，只从固定选项返回下一步建议，不联网、不读设备、不生成写入命令：

```powershell
python -m tianyi_electronics_skill advise --request "检查已有的离线波形"
```

输出包含 `mode: offline_rule_baseline`、`external_call_count: 0`、`advisory_only: true`、
`recommendations` 和空的 `device_errors`。建议项的 `may_write_device` 固定为 `false`。

## protocol

只读取本地文件并识别/解析厂商协议，不会连接设备：

```powershell
python -m tianyi_electronics_skill protocol identify --file payload.bin
python -m tianyi_electronics_skill protocol decode --file payload.bin --manifest-dir examples\protocols
```

- `identify` 返回 `selected` 与按置信度排序的 `candidates`；
- `decode` 成功时返回 `ok: true`、`frame`（含 `samples`、`sample_rate_hz`、`metadata`、
  `device_errors`）和 `device_errors`；
- 失败时返回 `ok: false`、`failures`，类别为 `parse_error` 或 `configuration_error`。

`capture-can` 只在有 SocketCAN 的 Linux 上使用可选依赖 `python-can`：

```powershell
python -m tianyi_electronics_skill protocol capture-can --channel can0 --count 16
```

更完整的协议/CAN 约束见 [protocols-and-bridges.md](protocols-and-bridges.md)。

## bridge

跨主机反向 TCP 桥接，要求中继、VPN 或 SSH 隧道可达：

```powershell
python -m tianyi_electronics_skill bridge serve --listen 0.0.0.0:9000
python -m tianyi_electronics_skill bridge expose --server relay:9000 --token <token> --local 127.0.0.1:5025
python -m tianyi_electronics_skill bridge connect --server relay:9000 --token <token> --listen 127.0.0.1:5025
```

`serve` 阻塞运行；`expose` 在设备侧启动，`connect` 在 Agent 侧启动。token 是访问凭据，
不要写入仓库、日志或报告。桥接只转发字节，不假设对端一定说 SCPI。
