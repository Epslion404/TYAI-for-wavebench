# 自定义设备与协议支持 TODO

配套规范：[自定义设备与协议支持开发规范](WaveBench_自定义设备与协议支持规范.md)。状态用 `[ ]` 未开始、`[~]` 进行中、`[x]` 完成。

## 平台跟踪（执行状态以 issue 为准）

本页是**契约与验收清单**（稳定事实）；日常执行状态、讨论与进度看 tyai 的 issue：

- 方向：[#13 [Direction] 自定义设备与协议支持](https://github.com/Epslion404/TYAI-for-wavebench/issues/13)
- 里程碑：`M1 传输层`／`M2 协议层`／`M3 设备层`／`M4 作品包同步`／`M5 交付`
- 过滤视图：`is:open label:track:device milestone:"M1 传输层"`（在 issue 搜索框里直接用）
- 规则：**里程碑收尾时才回来更新勾选框**；平时不要在文件里勾进度，避免与 issue 漂移。

## 已提 issue（比赛仓库）

核心侧工作统一在比赛仓库 `Epslion404/TYAI-for-wavebench` 跟踪，本页保留决策、顺序与验收记录；fork `Epslion404/wavebench` 留有同内容副本（编号相反），上游 `Scaxlibur/WaveBench` 不保留同类 issue。

| issue（比赛仓库） | 对应里程碑 | fork 副本 |
| --- | --- | --- |
| [#6 传输后端可扩展（CAN/CAN-FD、通用串口、可选 UDP）](https://github.com/Epslion404/TYAI-for-wavebench/issues/6) | M1 | [#1](https://github.com/Epslion404/wavebench/issues/1) |
| [#5 原始字节收发合同（按帧读写）](https://github.com/Epslion404/TYAI-for-wavebench/issues/5) | M2 | [#2](https://github.com/Epslion404/wavebench/issues/2) |
| [#4 串口连接配置从 DmmConfig 提升为通用连接配置](https://github.com/Epslion404/TYAI-for-wavebench/issues/4) | M1 | [#3](https://github.com/Epslion404/wavebench/issues/3) |
| [#3 声明式自定义协议描述（非 SCPI 帧定义）](https://github.com/Epslion404/TYAI-for-wavebench/issues/3) | M2 | [#4](https://github.com/Epslion404/wavebench/issues/4) |
| [#2 裁决是否新增设备 kind（如 bus_device）](https://github.com/Epslion404/TYAI-for-wavebench/issues/2) | M0／M3 | [#5](https://github.com/Epslion404/wavebench/issues/5) |
| [#1 枚举串口与 CAN 适配器，并标注端口协议](https://github.com/Epslion404/TYAI-for-wavebench/issues/1) | M1 | [#6](https://github.com/Epslion404/wavebench/issues/6) |

**范围假设（需确认）**：需求中的「窗口设备」按**串口设备**理解（与「CAN 总线设备」并列的现场总线／有线接口）。若原意是网口设备或其它含义，先修正本行再执行 M1。

## M0 决策与合同冻结（2026-10-02 完成）

- [x] 明确第一版范围：先做**串口 + 自定义协议设备**，再做 **CAN／CAN-FD 设备**；不做项见 RFC §6（不新增 kind、不引入默认安装依赖、无回读能力不开写）。
- [x] 写 RFC 并登记状态：[自定义设备与协议支持 RFC](../rfcs/WaveBench_自定义设备与协议支持RFC.md) 已提交，并在 [RFC 索引](../../rfcs/index.md) 的 `Draft` 段登记。
- [x] 裁决设备类别：**首版不新增 kind**，复用现有 kind + descriptor 扩展 + 新 capability；不属于任何既有类别的设备首版只走只读探测与原始帧读取（不进 run plan）。依据见 RFC §2.2。
- [x] 裁决串口参数归属：**新增 `SerialConnectionConfig`**，`_open_transport` 只认它，`DmmConfig` 负责构造它（DMM 行为零变化、回归锁定）。依据见 RFC §2.3。
- [x] 明确非 SCPI 设备的证据与恢复等价物：必须显式声明可读回字段与未覆盖项；没有回读能力的设备默认只读。依据见 RFC §2.4。
- 附加冻结：重放／重试分类（RFC §2.5）、依赖策略（RFC §2.6）。
- 实现按 RFC §4 分期；阶段 1（`SerialConnectionConfig` + 后端查表分派）可开工。

## M1 传输层

- [~] `_open_transport` 支持新后端与分派：`can`，串口通用化；如 RFC 通过再加 `udp`／裸 `socket`。
  - 进展（2026-10-02）：**查表分派已落地**（`TRANSPORT_FACTORIES` + `TransportRequest`，RFC 阶段 1）；未声明的后端仍在 I/O 前抛 `ConfigError`（有测试锁定）。`can` 后端待阶段 4。
  - 触点：`src/wavebench/instruments/factory.py::_open_transport`、`_select_backend`、`descriptor.backends`。
  - 验收：未声明的后端仍必须在真实 I/O 前抛 `ConfigError`；既有后端行为零变化。
- [x] 串口配置去 DMM 专用化：`serial_config: DmmConfig` 改为通用串口配置。
  - 进展（2026-10-02）：**类型已通用化**——新增 `SerialConnectionConfig`，`_open_transport`／`SerialTransport.open` 只认它，`DmmConfig.serial_connection()` 负责转换，DMM 路径行为不变（回归测试锁定）。
  - 已决（2026-10-02，追认实现）：scope／source／power 通过 `[connection]` 声明串口；`connection.backend = "serial"` 时由 Core 工厂把 `ConnectionConfig.serial_connection(resource=...)` 交给描述符声明的 driver（RFC §7 第 2 项，实现见 [#4](https://github.com/Epslion404/TYAI-for-wavebench/issues/4) 对应的合并 `a0a2b28`）。
  - 触点：`src/wavebench/instruments/factory.py`、`src/wavebench/transport/serial_transport.py`、`src/wavebench/config.py`。
  - 验收：DMM 串口路径回归通过；scope／source／power 在 descriptor 声明 `serial` 后可打开串口。
- [ ] CAN／CAN-FD 传输实现，依赖进可选 extra（默认安装不引入）。
  - 触点：`src/wavebench/transport/`（新文件）、`pyproject.toml`（optional-dependencies）、`src/wavebench/instruments/factory.py`。
  - 验收：无适配器时给出结构化错误；用 `python-can` 的 virtual 后端可跑通离线测试；比特率／采样点／FD 标志非法时在 I/O 前失败。
- [ ] 设备枚举与发现：串口枚举、CAN 适配器枚举，并明确与 `discovery.py` 现有 TCP 扫描边界的关系。
  - 触点：`src/wavebench/discovery.py`、`src/wavebench/doctor.py`、CLI `discover`。
- [ ] 测试矩阵：打不开、超时、半帧、校验失败、总线错误、会话污染、close 幂等。
  - 触点：`tests/`（参考 `test_transport_replay_matrix.py`、`test_transport_contracts.py`、`test_serial_transport.py`）。

## M2 协议层

- [x] 原始字节收发合同：`InstrumentTransport` 增 `send_frame`／`read_exact`／`read_until`（#5，master `f7ce0a6`），复用 `ReplayPolicy`、session 健康与租约。
  - 串口实现离线覆盖拆包、定界符、超限、部分超时与重放分类；只读模式下由 guarded 层在后端 I/O 前拦下；其它后端调用时报 `raw_frame_unsupported`。
  - 合并后待补的文档与校验见 [#28](https://github.com/Epslion404/TYAI-for-wavebench/issues/28)。
  - 触点：`src/wavebench/transport/base.py`、`transport/contracts.py`、`transport/guarded.py`、`transport/session.py`。
- [ ] 自定义帧定义：定长、长度字段、分隔符、转义、校验和（CRC16 等）、最大帧长、帧间隔与响应超时。
  - 验收：帧格式非法／超长／校验失败必须结构化失败，不得当作有效响应。
- [ ] 声明式协议描述（TOML），形状参考现有声明式 SCPI 插件。
  - 触点：`src/wavebench/plugins/scpi.py`、`docs/project/reference/plugins/scpi-plugin.example.toml`。
- [x] 重试与重放策略分类：合同已在 RFC §2.5 冻结（幂等只读 `SAFE_TO_REPLAY`、写／触发 `NO_REPLAY`、二进制采集 `READ_CONTINUATION_ONLY`）；代码落地随阶段 2。
  - 触点：`src/wavebench/transport/contracts.py`、`docs/project/rfcs/WaveBench_transport重放与session健康RFC.md`。

## M3 设备层接入

- [ ] descriptor 声明协议、资源 scheme 与 capability；driver 只做帧映射与解析。
- [ ] config／CLI／doctor 接入：`--resource` 覆盖、配置校验、只读探测（首版只读优先）。
  - 触点：`src/wavebench/config.py`、`cli_parser.py`、`cli.py`、`doctor.py`。
- [ ] 执行路径：run plan step、artifact、报告；首版若不做，必须在本节写明不支持范围与拒绝方式。
  - 触点：`src/wavebench/services/run_plan.py`、`services/run_artifacts.py`、`report/`。
- [ ] 安全与恢复：状态快照／恢复或显式标注未覆盖；写操作沿用 access policy 与写后回读。

## M4 比赛作品包同步

- [ ] `比赛区/天翼AI电子科研Skill/src/tianyi_electronics_skill/` 扩展 `discover`／`inspect`／`measure`／`analyze` 支持新设备类别。
- [ ] 更新 `SKILL.md`、`README.md`、`agents/openai.yaml`，并补 `examples/` 离线示例（无硬件可跑）。
- [ ] 同步内嵌快照 `比赛区/天翼AI电子科研Skill/wavebench/`（整目录覆盖，勿手改单文件）。
  - 验收：快照与根目录同名文件逐字节一致。
  - ~~重新打包 zip~~ 已作废：按[仓库协作与分支约定](仓库协作与分支约定.md)，zip 不再入库，打包产物改走 GitHub Release。

## M5 验证与交付

- [ ] 离线三件套：`python -m pytest -q`、`python -m ruff check .`、`python scripts/generate_docs.py --check`，外加 `audit_docs.py --quiet-warnings`。
- [ ] 实机验收清单（单独授权后执行）：IDN、接线、初态、写前快照、回读、未恢复项、产物目录。
- [ ] 文档同步：canonical Reference（配置／CLI／run schema／artifact／插件）+ `CHANGELOG.md`。
- [ ] 提交推送（GitHub 操作手动带 `http.proxy=http://127.0.0.1:7897`）。

## 风险与待确认

- CAN 依赖与 Windows 驱动：`python-can` 需要适配器驱动／DLL，离线只能覆盖 virtual 后端；实机验收不可替代。
- 新增 kind 的成本主要在 CLI／run plan／报告的 kind 分派，若不新增 kind，需确认「同 kind 不同协议」不会让 capability 校验产生歧义。
- 原始字节收发合同一旦发布即为公共合同，需在 RFC 中按 append-only 方式冻结，避免后续反复改签名。
