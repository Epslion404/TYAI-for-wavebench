# 自定义设备与协议支持 RFC

- 状态：`Draft`（2026-10-02）
- 目标：让 WaveBench 以**插件与声明式描述**接入非 SCPI 设备与自定义协议（串口设备、CAN／CAN-FD、私有帧协议），而不把总线细节或厂商差异写进 Core 业务层。
- 关联：tracker 上的 `#1`／`#2`／`#3`／`#4`／`#5`／`#6`；现状基线与执行清单见 [自定义设备与协议支持规范](../design/WaveBench_自定义设备与协议支持规范.md) 与 [同名 TODO](../design/WaveBench_自定义设备与协议支持TODO.md)。
- 说明：本文件在 `Draft` 状态下不构成对外承诺；「实现状态」以落地后的 Reference 与提交为准。

## 1. 现状基线（改动前可核对的源码事实）

| # | 事实 | 位置 |
| --- | --- | --- |
| 1 | 传输后端在 Core 内硬编码，未知值直接抛 `ConfigError` | `src/wavebench/instruments/factory.py::_open_transport` |
| 2 | 插件拿不到自定义传输：`open_transport()` 由 Core 注入，且一次 factory 只允许一个传输 | `instruments/api.py::DriverContext`、`factory.py` 的 "more than one transport" 分支 |
| 3 | 串口只对 DMM 打开：串口参数（波特率／数据位／校验／停止位／流控／终止符）定义在 `DmmConfig` 上 | `config.py::DmmConfig`、`factory.py::_open_transport(serial_config: DmmConfig)` |
| 4 | 传输协议面是 SCPI 语义，没有原始字节读取 | `transport/base.py::InstrumentTransport` |
| 5 | 已有二进制合同只覆盖「ASCII 命令 + 确定块／消息」 | `transport/contracts.py::BinaryQueryResult` |
| 6 | 设备类别是固定六元组，CLI／config／service／run plan／报告按 kind 分派 | `plugins/api.py::VALID_PLUGIN_KINDS` |
| 7 | 后端与资源 scheme 由 descriptor 声明并校验 | `instruments/api.py::InstrumentDescriptor.backends/resource_schemes` |
| 8 | 已有声明式先例可对齐形状 | `plugins/scpi.py::load_scpi_plugin`、`docs/project/reference/plugins/scpi-plugin.example.toml` |
| 9 | 发现路径只扫 TCP/SCPI 端口 | `discovery.py`（`DEFAULT_DISCOVERY_PORTS`、`SCPI_SOCKET_PORTS`） |

## 2. 裁决

### 2.1 第一版范围

先做**串口设备 + 自定义协议**，再做 **CAN／CAN-FD**：两者共用同一套「传输后端 + 按帧读写 + 声明式协议」合同，第二阶段只是多一个后端实现与可选依赖，不需要新概念。

### 2.2 设备类别：首版**不新增** `PluginKind`

- 复用现有 kind + descriptor 扩展 + 新的 capability 命名空间；设备若本质是示波器／信号源／电源／万用表，就用对应 kind。
- 对**不属于任何既有类别**的设备（例如纯 CAN 采集器），首版只支持「只读探测 + 原始帧读取 + 离线分析」，**不进入 run plan、不写 artifact**，并在输出里显式标注 `unsupported`。
- 理由：新增 kind 牵动 config 校验、CLI parser／handler、service、run plan schema、artifact 与报告分派（源码事实 6），是 Core 全链路改动；先把传输与协议合同做对，等出现第二个真实的设备类别需求再单独立项（届时按本仓 RFC 流程改 `VALID_PLUGIN_KINDS`）。

### 2.3 串口参数归属：新增 `SerialConnectionConfig`

- 新增共享的 `SerialConnectionConfig`（`port`／`baudrate`／`bytesize`／`parity`／`stopbits`／`timeout_ms`／`write_termination`／`read_termination`／`xonxoff`／`rtscts`／`dsrdtr`）。
- `_open_transport` 只接受 `SerialConnectionConfig`；`DmmConfig` 保留对外字段并负责构造它（DMM 行为零变化，回归测试锁定）。
- descriptor 声明 `backend="serial"` + 资源 scheme（`serial`）即可打开串口，不再依赖 DMM 专有形状。

### 2.4 证据与恢复等价物

- 非 SCPI 设备必须**显式声明**「可读回的字段」与「不可恢复项」；沿用既有 access policy、写前快照与写后回读语义。
- **没有回读能力的设备默认只读**：任何写操作在 capability 校验阶段被拒，不做「写后不校验」的例外。
- 状态恢复按「声明的字段子集 + 未覆盖清单」写入产物；无法恢复的字段必须在 artifact 与报告中列出。

### 2.5 重试与重放策略分类

| 操作 | 策略 | 说明 |
| --- | --- | --- |
| 幂等只读查询（文本／数值） | `SAFE_TO_REPLAY` | 会话健康受损时可重放 |
| 写、输出切换、触发、清错误队列 | `NO_REPLAY` | 不盲目重试；结果不明时保持输出 OFF 并停止后续写入 |
| 二进制／分帧采集 | `READ_CONTINUATION_ONLY` | 只有读续传可重试，命令本身不重发 |

### 2.6 依赖策略

`python-can` 等总线依赖进**可选 extra**，默认安装不引入；`plugin doctor` 报告缺失但不联网；测试用虚拟总线与假传输，不依赖真实适配器。

## 3. 拟新增的公共合同

1. **按帧读写（append-only）**：在 `InstrumentTransport` 上新增 `send_frame(bytes)`／`read_exact(n)`／`read_until(delimiter)`，复用既有 `ReplayPolicy`、session 健康与 resource lease；既有方法与签名不变。
2. **后端分派查表**：`_open_transport` 改为「backend → 构造器」查表，`descriptor.backends` 决定可用集合；未声明的后端仍在真实 I/O 前失败。
3. **声明式协议描述（TOML）**：`framing`（`definite-block`／`delimited`／`fixed`／`length-field`）、`magic_hex`、`header_bytes`、`length_field`（偏移／宽度／字节序）、`checksum`（`none`／`crc16/arc` 等）、`max_frame_bytes`、`inter_frame_ms`、`response_timeout_ms`、`sample_type`／`endian` 白名单、帧字段到 payload 的映射。形状对齐现有声明式 SCPI 插件与技能包已实现的 JSON manifest。
4. **资源 scheme**：串口沿用 VISA 风格的 `ASRL::`（或 `serial://`），CAN 用 `can://<channel>`；descriptor 通过 `resource_schemes` 声明。

## 4. 分期实施

| 阶段 | 内容 | 依赖 |
| --- | --- | --- |
| 1 | `SerialConnectionConfig` + `_open_transport` 查表分派（行为等价，DMM 回归） | 2.3 |
| 2 | 按帧读写合同 + 假传输测试（超时／半帧／校验失败／会话污染／close 幂等） | 阶段 1 |
| 3 | 声明式协议描述 + 帧校验接入 descriptor | 阶段 2 |
| 4 | CAN／CAN-FD 后端（可选 extra + 虚拟总线测试） | 阶段 1、2 |
| 5 | 只读探测与 `doctor`／`discover` 接入（不依赖 kind 的路径） | 阶段 3 |
| 6 | 文档与验收（canonical Reference、CHANGELOG、实机验收清单） | 全部 |

## 5. 验收门

- 未声明的后端仍必须在真实仪器 I/O 之前失败；既有 SCPI 路径**零行为变化**（`tests/test_transport_*`、`test_serial_transport.py`、DMM 相关用例全绿）。
- 帧格式非法、超长、校验失败、响应读到一半超时 → 结构化失败，不得当作有效数据；二进制采集不得使用普通读取重试。
- 无回读能力的设备默认只读；写操作必须有显式 capability 授权与写后回读。
- 全程离线可测（假传输 + 虚拟总线）；实机验收必须单独授权。
- 首版不新增 `PluginKind`，因此 CLI／run plan／报告的 kind 分派零改动。

## 6. 不做的事

- 不在首版新增 `PluginKind`，也不把 CAN／串口细节写进 Core 业务层。
- 不引入默认安装的第三方依赖；没有回读能力时不开放写路径。
- 不把比赛作品包内容带进上游（见[仓库协作与分支约定](../design/仓库协作与分支约定.md)）。

## 7. 待评审项

- 2.2 的「不新增 kind」是否接受（若否，需要额外的 Core 全链路改动清单）。
- 2.3 的串口配置命名与是否把串口参数也放到 `[connection]`（首版倾向放在 descriptor／插件侧，Core 只收 `SerialConnectionConfig`）。
- CAN 的比特率／采样点与终端电阻等硬件约束是否需要在 Core 侧做前置校验（倾向：只在 descriptor 声明 + doctor 报告，不替用户判断硬件）。
