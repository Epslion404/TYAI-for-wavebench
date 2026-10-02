# 新增仪器驱动

本页适用于将新仪器接入 WaveBench Core 执行路径。目标是保持「driver 表达设备差异，Service 表达实验动作」的边界，并让 CLI、doctor、run plan、artifact 和测试有明确的接入顺序。

## 接入顺序

1. 确认手册和最小只读操作，例如身份、状态和错误查询。
2. 编写 driver，只封装设备命令和响应解析。
3. 编写 Service，明确读取、写入、输出和恢复的业务动作。
4. 增加 config、CLI parser 和 CLI handler。
5. 接入只读 `doctor` 与 `run verify`。
6. 只有真实用户任务需要时，再增加 run plan step、artifact、模板和报告支持。
7. 为每一层补 fake transport、service、CLI、schema 和失败路径测试。

## 原始帧路径（非 SCPI 设备）

需要「发一帧、收一帧」的私有协议（长度字段、校验和、二进制定界）走 transport 的原始帧接口，而不是把帧塞进 SCPI 查询：

| 操作 | 用途 | 重放策略 |
| --- | --- | --- |
| `send_frame(frame, max_bytes=...)` | 发送一帧写入 | 只接受 `NO_REPLAY` |
| `read_exact(size, timeout_ms=...)` | 读满固定字节数 | 拒绝 `SAFE_TO_REPLAY`；二进制读取用 `READ_CONTINUATION_ONLY` |
| `read_until(delimiter, max_bytes=...)` | 读到定界符为止 | 同上 |

- 三个操作都由 guarded transport 统一门控：access policy、资源租约、会话健康与计数器（`binary_query_calls`／`blocked_binary_query_calls`）照常生效；部分读会把会话标记为不健康（`synchronization=LOST`）。
- 超限、非法定界符、错误的重放策略都在**传输前**被拒绝（`attempts=0`）。
- **原始帧目前只有串口后端实现**；其它后端调用时会得到 `raw_frame_unsupported`，而不是 `AttributeError`。

## 不变量

- driver 不读取 CLI 参数，也不直接写 run artifact。
- 不默认 reset、autoscale、触发采集或切换输出。
- 写入动作必须有明确命令或 run step，并在需要时经 capability、access policy 和安全限制门控。
- 型号私有 SCPI、profile、quirk 和实机 evidence 属于 instrument plugin 仓库。

## 相关页面

- [插件开发](plugin-development.md)
- [测试 WaveBench](testing.md)
- [配置 Reference](../reference/configuration.md)
- [run plan Reference](../reference/run-schema.md)
