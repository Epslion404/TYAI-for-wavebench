# 协议识别、CAN 与跨主机桥接

读取时机：使用 `protocol`、`capture-can`、`bridge`，或需要解释发现结果中的协议标签时读本页。
普通 SCPI 测量不需要本页。

## 网络发现与协议标签

`discover` 默认探测端口 `5025,5555,4000,502`：

- `5025`、`5555` 按 SCPI/TCP 处理，会尝试 `*IDN?`；
- `502` 提示 Modbus/TCP，不把响应当成仪器；
- `4000` 标为通用 instrument TCP；
- 其他端口可能标为 `vxi11-candidate` 或未知。

`--probe-non-scpi` 会扩大 `*IDN?` 尝试范围；`--mdns` 额外查询 `_scpi._tcp.local`。
默认 IPv4 目标数上限 4096；超过时用 `--force` 前先确认扫描范围。IPv6 仍受上限保护。
协议标签只是端口惯例或候选提示，不等于已完成设备能力确认，也不能据此开放写操作。

## protocol identify/decode

这两个命令只读本地文件，不会连接设备：

```powershell
python -m tianyi_electronics_skill protocol identify --file payload.bin
python -m tianyi_electronics_skill protocol decode --file payload.bin --manifest-dir examples\protocols
```

支持的适配器：

- **JSON 波形**：读取 `samples`、`sample_rate_hz` 和可选的 `metadata`/`device_errors`；
- **CAN 帧**：接受 `can_id` 或 `arbitration_id`、`data`/`data_hex`；DLC=0 规范化为
  `samples: []`，不会补零；
- **厂商二进制 manifest**：`kind` 必须是 `binary-waveform`，按 manifest 的 magic、header、
  字节序和缩放解码。

`identify` 的 `selected` 是最高置信度匹配，`candidates` 保留排序；`decode` 成功后返回
`ok: true` 和 `frame`，失败返回 `ok: false` 和 `failures`。没有 manifest 时无法猜出任意厂商协议，
应补充 manifest 或只做原始十六进制检查。波形分析至少需要 4 个有限采样点；少于 4 点会直接拒绝。

示例 manifest 位于 `examples/protocols/`；完整协议字段和扩展方式见内嵌
`wavebench/docs/` 中的协议文档。

## CAN

`protocol capture-can` 使用 Linux SocketCAN 和可选依赖 `python-can`：

```powershell
python -m tianyi_electronics_skill protocol capture-can --channel can0 --count 16 --timeout 1.0
```

它返回 `frames`，每帧包含 `can_id`、`data`、`timestamp`。该命令没有设备错误队列，
`device_errors_status` 为 `not_supported_for_can_capture`。Windows 或没有 SocketCAN 的环境
不要假装 CAN 可用；需要原始帧时先确认适配器和接口名。

CAN 帧不是字节流仪器：不要在 CAN 上直接套用 SCPI 的 `read_until` 或分隔符协议。需要协议路由时，
使用带 arbitration ID/DLC 语义的 manifest，或把原始帧交给离线分析。

## bridge

桥接用于“设备主机”和“Agent 主机”不在同一局域网、但存在可达中继的场景。它只转发 TCP 字节，
不要求对端一定说 SCPI，也不替代 VPN、SSH 隧道或中继本身。

```powershell
# 中继主机
python -m tianyi_electronics_skill bridge serve --listen 0.0.0.0:9000

# 设备主机：把本机 5025 暴露到中继
python -m tianyi_electronics_skill bridge expose --server relay.example:9000 --token <token> --local 127.0.0.1:5025

# Agent 主机：在本机建立可连接的入口
python -m tianyi_electronics_skill bridge connect --server relay.example:9000 --token <token> --listen 127.0.0.1:5025
```

- `serve` 阻塞运行；`expose`/`connect` 需要可达的中继地址和相同 token；
- token 是访问凭据，不要提交到仓库、写进日志或粘贴到报告；
- 桥接成功后仍按普通 `inspect`/`measure`/`fetch` 访问 `127.0.0.1:5025`；
- 桥接失败不要反复重连写操作，先确认中继、防火墙和监听端口。

## 与完整 WaveBench 的边界

本包装层的 `protocol`/`bridge` 是离线与演示入口。完整 WaveBench 的插件、run plan、报告、
更多传输后端和协议扩展位于 `wavebench/`；需要高级功能时先读那边的文档，不要把本页的
限制当成完整 WaveBench 的能力清单。
