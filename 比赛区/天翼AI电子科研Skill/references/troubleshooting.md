# 故障排查

读取时机：命令失败、输出不符合预期，或需要判断“能不能重试/能不能写入”时读本页。错误类别和
安全边界以 [safety-and-errors.md](safety-and-errors.md) 为准。

## 先看三处

1. `ok`：为 `false` 表示本次命令失败；普通命令退出码通常为 `2`。
2. `failures`：看 `category`、`command`、`message`，不要只看最后一层异常文本。
3. `device_errors`：设备错误队列；它可能为空，即使 Agent 侧已经失败。

同时保留完整 JSON 和时间戳。不要删掉失败条目后重跑，否则无法区分“设备没响应”和“本地解析错”。

## 常见症状

### discover 没有设备

- 确认 `--subnet` 与设备实际网段一致；不要用容器内网地址猜设备地址。
- 检查端口是否被防火墙拦截；先试 `--ports 5025` 减少变量。
- 非 SCPI 端口可能只给协议提示，不会出现在仪器列表；需要时加 `--probe-non-scpi`。
- 超过 4096 个 IPv4 目标会被拒绝；确认范围后才使用 `--force`，IPv6 不受 `--force` 绕过。
- 只有 mDNS 设备时加 `--mdns`；mDNS 失败不应被当成仪器故障。

### inspect/measure 连接失败

- `transport_error`/`timeout`：检查 IP、端口、网线/路由和桥接；不要靠重复写命令修复。
- `unsupported`：设备可能使用不同方言。`measure` 会尝试两种常见命令；查看 `unsupported` 中的
  每次尝试，再决定是否换通道或换只读方法。
- `error_queue_unavailable`：本次结果未验证；不要因为拿到了数值就声称成功。
- 某通道 `display: null`：设备拒绝或返回空响应；这是状态未知，不是关闭状态。

### 测量值为 null 或缺失

- 先看 `measurements` 是否真的有该字段；缺失时看 `unsupported`/`failures`。
- `no_response` 可能是触发未发生、通道关闭、传输格式不对或命令不支持。
- `parse_error` 常见于 ASCII/REAL 格式不一致、块头非法或响应不是数值；检查 `--format`。
- 数值为 `null` 不等于 0 V；报告中必须保留未知。

### fetch 没有样本

- `sample_count=0` 时不要生成波形或指标；先看 `errors`/`failures`。
- `header_consistent=false` 时用 `--sample-rate` 显式指定采样率，或保留 warning 并说明限制。
- `real` 格式解析失败时确认设备返回 IEEE 488.2 确定块；必要时改用 `--format ascii` 做只读诊断。
- 只读不写入时不要用 `autoscale --apply` 来“修复”采集。

### autoscale 失败或只成功一部分

- 退出码 `2`：没有验证任何写入，或执行/清理失败；不要重复 `--apply`。
- 退出码 `3`：部分设置已验证应用；先读 `write_results`、`failures`、`device_errors`，再让用户
  决定恢复或继续。
- 退出码 `0` 只代表建议完成或全部设置验证完成；仍要检查 `failures` 是否记录了清理错误。
- 写入后回读不一致记为 `verification_failed`；不要把它降级成 warning。

### protocol identify/decode 失败

- 没有匹配项时检查 `--manifest-dir` 是否指向包含 manifest 的目录。
- `configuration_error` 通常是 manifest 字段、采样率或文件参数非法；先修参数。
- `parse_error` 可能是 magic/header/字节序/缩放不匹配；用十六进制查看原始 payload。
- 少于 4 个样本不能做波形分析；空样本是合法结果，不是补零理由。
- 不要为了让 decode 通过而修改原始数据。

### CAN 或 bridge 不可用

- `capture-can` 需要 Linux SocketCAN 和 `python-can`；Windows 不要假装可用。
- bridge 需要可达的中继、正确 token 和一致的监听端口；先确认 `serve` 已启动。
- token 不要写入仓库、日志或报告；桥接失败时先只读检查网络，不要反复重发写命令。

## 重试规则

- 只读查询可以换参数/换会话后重试；先记录上一次失败。
- 超时和不完整响应后必须关闭旧会话，新建会话再通信；不要复用可能已错位的流。
- 写入、`autoscale --apply`、协议配置变更不要自动重试；先确认设备状态和用户授权。
- 同一问题连续失败三次仍没有新证据时，停止重试，向用户报告已尝试内容、错误类别和需要的信息。

## 交付前检查

- `failures` 是否为空；非空时是否已解释每个类别。
- `device_errors` 是否已读取；读不到时是否标记为未验证。
- 是否保留了完整 JSON 和原始波形文件。
- 报告是否写清未执行、未验证和需要人工恢复的项。
