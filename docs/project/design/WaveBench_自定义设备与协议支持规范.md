# 自定义设备与协议支持开发规范

本页是 `competition` 分支上的开发规范，用于接入**非 SCPI 的自定义设备**（串口设备、CAN／CAN-FD 设备等）与**自定义协议**（二进制帧、私有文本协议）。它不是用户文档：当前能力以 CLI、`run schema`、插件 descriptor 和 Reference 为准。按里程碑推进的执行清单见 [自定义设备与协议支持 TODO](WaveBench_自定义设备与协议支持TODO.md)。

## 目标与非目标

- 目标：新设备的收发、配置、诊断、执行、产物和安全门都走 Core 既有路径，能被 `doctor`、run plan、artifact、报告和恢复机制复用。
- 非目标：不做绕过 Core 的旁路脚本（例如直接 `python-can` 读数据后自己写 CSV）；不把某个型号的帧定义写进 Core；不因为「新设备不支持某能力」而放宽既有安全门。

## 现状基线（改前必须核对的事实）

> 进展（2026-10-02）：下面第 3、4 项已随 M1 阶段 1（[#4](https://github.com/Epslion404/TYAI-for-wavebench/issues/4)）与 M2 阶段 2（[#5](https://github.com/Epslion404/TYAI-for-wavebench/issues/5)）落地而改变——串口参数已进 `[connection]`（RFC §7 第 2 项已裁决），`InstrumentTransport` 已有 `send_frame`／`read_exact`／`read_until`。当前事实以代码与[配置 Reference](../../reference/configuration.md)为准；以下列表保留改前的核对基线。

1. 传输后端在 Core 内硬编码：`src/wavebench/instruments/factory.py::_open_transport` 只接受 `serial`、`pyvisa`、`rsinstrument`、`rsinstrument-socket`、`rsinstrument-rsvisa`、`rsinstrument-pyvisa-py`，其它值抛 `ConfigError`。
2. 插件拿不到自定义传输：`src/wavebench/instruments/api.py::DriverContext.open_transport` 由 Core 注入，且一次 factory 只允许打开一个传输（`factory.py` 的 "more than one transport" 分支）。
3. 串口目前只对 DMM 打开：`_open_transport(serial_config: DmmConfig)` 是唯一入口，波特率／数据位／校验／停止位／流控／读写终止符定义在 `src/wavebench/config.py::DmmConfig`；`config.py` 只对 `dmm.backend` 允许 `serial`。
4. 传输协议面是 SCPI 语义：`src/wavebench/transport/base.py::InstrumentTransport` 只有 `write`、`write_bytes`、`query`、`query_float_list`、`query_bin_block`、`query_binary`、`query_opc`、`close`；没有原始字节读取，也没有「发送一帧—接收一帧」的接口。
5. 已有二进制分帧合同只覆盖「ASCII 命令 + 确定块／消息」：`src/wavebench/transport/contracts.py::BinaryQueryResult`（`DEFINITE_BLOCK`／`MESSAGE`，字节记账与 `Synchronization` 必须可证明），不能表达长度字段在外层、多帧重组或带校验和的私有帧。
6. 设备类型是固定六元组：`src/wavebench/plugins/api.py::VALID_PLUGIN_KINDS`（`scope`／`source`／`rf_source`／`power`／`dmm`／`sweep_analyzer`），CLI、config、service、run plan 与报告都按 kind 分派。
7. 后端与资源 scheme 由 descriptor 声明：`src/wavebench/instruments/api.py::InstrumentDescriptor.backends/resource_schemes`，由 `factory.py::_select_backend`、`_validate_resource_scheme` 校验（scheme 取资源串开头字母并转小写）。
8. 已有声明式先例：`src/wavebench/plugins/scpi.py::load_scpi_plugin` 用 TOML 描述 SCPI 插件（见 `docs/project/reference/plugins/scpi-plugin.example.toml`），可作为声明式自定义协议插件的形状参考。
9. 发现路径只扫 TCP／SCPI：`src/wavebench/discovery.py` 的 `DEFAULT_DISCOVERY_PORTS = (5025, 5555, 111)`、`SCPI_SOCKET_PORTS = {5025, 5555}`，资源串形如 `TCPIP::<ip>::<port>::SOCKET`。
10. 安全、证据、恢复都建立在 SCPI 回读之上：`src/wavebench/services/access_policy.py`、`services/resource_lease.py`、`transport/session.py`、`instruments/source_restore.py`。

以上任一条在开发中发生变化，先更新本页再动代码。

## 分层扩展点

| 层 | 回答的问题 | 落点 |
| --- | --- | --- |
| 传输 | 字节怎么进出（TCP／UDP、串口、CAN／CAN-FD、VISA） | `transport/` 新增实现 + `factory.py` 后端分派 |
| 协议 | 帧怎么定义、怎么分帧与校验、请求如何对应响应 | 新增原始字节收发合同 + 声明式协议描述 |
| 设备 | 设备是什么、有哪些能力、怎么配置、怎么执行与恢复 | `instruments/` descriptor／driver + `config.py` + service／CLI |

## 不变量（违反即返工）

- **先声明，后打开**：缺 capability 或配置不匹配时必须在真实仪器 I/O 之前失败，见 [设备抽象](../../concepts/device-abstraction.md)。
- descriptor 导入过程不得做 I/O、端口扫描、写文件或改全局状态；driver 不读 CLI 参数、不写 run artifact、不隐式 reset／autoscale／trigger／开输出，见 [插件开发](../../development/plugin-development.md)。
- 写入动作必须有显式命令或 run step，并经过 capability、access policy 与安全上限门控，写后回读；禁止为通过测试静默放宽限制。
- 不复制厂商实现去覆盖 Core 约束：既有 `NO_REPLAY` 与结构化会话错误处理必须保留。
- 新合同 append-only：`InstrumentDescriptor` 的位置参数顺序不得改动，新增字段只能追加。
- 新的后端或协议不得让既有 SCPI 路径产生行为变化；回归以 `tests/` 现有矩阵为准。

## 开发流程（按顺序，不可跳步）

1. **RFC**：涉及新传输合同、原始字节收发合同或新增设备 kind 时，先在 `docs/project/rfcs/` 落 RFC，并更新 [RFC 索引](../../rfcs/index.md) 的状态表；参考 `docs/project/rfcs/WaveBench_transport重放与session健康RFC.md` 的记录方式。
2. **冻结合同**：capability 名称、帧字段、超时与重试语义、错误分类、恢复范围，写成可被测试断言的清单。
3. **离线实现 + fake 测试**：新后端与新协议必须能用 fake 传输覆盖正常、超时、半帧、校验失败、会话污染与关闭路径；参考 `tests/test_transport_replay_matrix.py`、`tests/test_transport_call_classification.py`、`tests/test_driver_structured_errors.py` 的组织方式。
4. **离线验收**：聚焦测试 → `python -m pytest -q` → `python -m ruff check .` → `python scripts/generate_docs.py --check` → `python .agents/skills/wavebench-docs/scripts/audit_docs.py --quiet-warnings`。
5. **实机验收**：必须单独授权，按 [wavebench Skill](../../../.agents/skills/wavebench/SKILL.md) 的安全门记录 IDN、接线、初态、写前快照、回读结果与未恢复项；离线通过不得写成实机验收。
6. **文档同步**：用户可见行为只写进 canonical Reference（CLI／配置／run schema／artifact／插件），开发线状态不写成已发布能力。
7. **提交**：`feat|fix|docs|test|chore|refactor|perf(scope): 说明`，范围小、可验证；`competition` 分支直接提交，面向母仓库的改动走 `master`。

## 本机环境与可用 skill

- GitHub 操作手动带代理：`git -c http.proxy=http://127.0.0.1:7897 push|fetch|ls-remote`，不写进 `git config`。
- 仓库不提交 `.venv`；本机系统 `python` 未必是 3.11／3.12，先建 venv 再装 `.[dev,analysis,tui]`。
- 本机基线（2026-09-29，Python 3.12.10 + `.[dev,analysis,tui,docs]`，勿把这些当成自己的改动）：全量 `pytest` 为 2459 passed／2 failed／4 skipped——`tests/test_analysis_execution.py::test_windows_job_rejects_allocation` 稳定失败（numpy 2.5.3 的 OpenBLAS 报内存分配失败而非 `MemoryError`，且错误记录无 `cause`），`tests/test_mcp_http.py::McpHttpTests::test_mcp_jsonrpc_requires_token` 只在全量并发跑时偶发 `WinError 10053`（单独跑 20 passed）。`ruff check .` 在比赛分支会因 `测试区/.temp/full_discover.py` 报 6 个错误（该脚本来自比赛提交，不在上游）。
- 推荐配合的 skill：`planning-with-files`（多阶段任务）、`tdd`（先写失败测试）、`systematic-debugging`／`diagnose`（协议与时序问题）、`opensrc` + `find-docs`／`get-api-docs`（确认 python-can／pyserial 等真实 API 与版本，不靠记忆）、`windows-shell`（命令与编码坑）、`hardware-design-flow`（CAN 收发器、终端电阻、电平这类硬件侧判断）、`humanizer-zh`（中文文档表达）；仓库内固定使用 `.agents/skills/wavebench`（安全门与开发验证）与 `.agents/skills/wavebench-docs`（文档治理）。

## 禁止事项

- 用裸 `python-can`／`pyserial` 脚本绕过 Core 的传输、租约、安全门与 artifact。
- 把真实仪器地址、适配器序列号、实验数据写进跟踪文件（`wavebench.toml`、`/data/`、`tool-of-rei/` 已 gitignore）。
- 在没有能力声明的情况下开放写操作，或让未知帧被当作成功响应。
- 把开发线状态写进用户 Reference，或把单一型号的帧格式写成通用合同。
