# 自定义设备与协议支持 TODO

配套规范：[自定义设备与协议支持开发规范](WaveBench_自定义设备与协议支持规范.md)。状态用 `[ ]` 未开始、`[~]` 进行中、`[x]` 完成。

**范围假设（需确认）**：需求中的「窗口设备」按**串口设备**理解（与「CAN 总线设备」并列的现场总线／有线接口）。若原意是网口设备或其它含义，先修正本行再执行 M1。

## M0 决策与合同冻结

- [ ] 明确第一版范围：先做**串口 + 自定义协议设备**，再做 **CAN／CAN-FD 设备**。
  - 验收：一页结论写入本节，含不做项。
- [ ] 写 RFC 并登记状态：通用传输后端、原始字节收发合同、是否新增设备 kind。
  - 触点：`docs/project/rfcs/`、`docs/rfcs/index.md`。
  - 依赖：无。这是后续所有改动的前置。
- [ ] 裁决设备类别：新增 kind（如 `bus_device`）还是复用现有 kind + descriptor 扩展。
  - 触点：`src/wavebench/plugins/api.py`（`VALID_PLUGIN_KINDS`、`PluginKind`）、`src/wavebench/instruments/api.py`（`InstrumentDescriptor.kind`）。
  - 影响：新增 kind 会牵动 config 校验、CLI parser／handler、service、run plan step、artifact、报告与 capability 校验，工作量远大于「复用 kind + 新 capability」。
- [ ] 裁决串口参数归属：`ConnectionConfig` 增加串口字段，还是新建 `SerialConnectionConfig` 并让 `DmmConfig` 复用它。
  - 触点：`src/wavebench/config.py`（`ConnectionConfig`、`DmmConfig`、`SCOPE_CONNECTION_BACKENDS` 校验）、`wavebench.example.toml`。
- [ ] 明确非 SCPI 设备的证据与恢复等价物：用什么读回证明状态、哪些字段声明为未覆盖。
  - 参考：`src/wavebench/instruments/source_restore.py` 的声明式思路（`supported`／`operations`／`fields`／`excluded_fields`）。

## M1 传输层

- [ ] `_open_transport` 支持新后端与分派：`can`，串口通用化；如 RFC 通过再加 `udp`／裸 `socket`。
  - 触点：`src/wavebench/instruments/factory.py::_open_transport`、`_select_backend`、`descriptor.backends`。
  - 验收：未声明的后端仍必须在真实 I/O 前抛 `ConfigError`；既有后端行为零变化。
- [ ] 串口配置去 DMM 专用化：`serial_config: DmmConfig` 改为通用串口配置。
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

- [ ] 原始字节收发合同：在不破坏现有 `InstrumentTransport` 的前提下增加按帧读写（如 `send_frame`／`read_exact`／`read_until`），并复用 `ReplayPolicy`、session 健康与租约。
  - 触点：`src/wavebench/transport/base.py`、`transport/contracts.py`、`transport/guarded.py`、`transport/session.py`。
- [ ] 自定义帧定义：定长、长度字段、分隔符、转义、校验和（CRC16 等）、最大帧长、帧间隔与响应超时。
  - 验收：帧格式非法／超长／校验失败必须结构化失败，不得当作有效响应。
- [ ] 声明式协议描述（TOML），形状参考现有声明式 SCPI 插件。
  - 触点：`src/wavebench/plugins/scpi.py`、`docs/project/reference/plugins/scpi-plugin.example.toml`。
- [ ] 重试与重放策略分类：明确哪些帧 `SAFE_TO_REPLAY`、哪些 `NO_REPLAY`；二进制采集不得用普通读取重试。
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
- [ ] 同步内嵌快照 `比赛区/天翼AI电子科研Skill/wavebench/`（整目录覆盖，勿手改单文件）并重新打包 `比赛区/天翼AI电子科研Skill.zip`。
  - 验收：快照与根目录同名文件逐字节一致（`tool-of-rei/`、`skills/` 差异除外）；zip 解包内容与目录一致。

## M5 验证与交付

- [ ] 离线三件套：`python -m pytest -q`、`python -m ruff check .`、`python scripts/generate_docs.py --check`，外加 `audit_docs.py --quiet-warnings`。
- [ ] 实机验收清单（单独授权后执行）：IDN、接线、初态、写前快照、回读、未恢复项、产物目录。
- [ ] 文档同步：canonical Reference（配置／CLI／run schema／artifact／插件）+ `CHANGELOG.md`。
- [ ] 提交推送（GitHub 操作手动带 `http.proxy=http://127.0.0.1:7897`）。

## 风险与待确认

- CAN 依赖与 Windows 驱动：`python-can` 需要适配器驱动／DLL，离线只能覆盖 virtual 后端；实机验收不可替代。
- 新增 kind 的成本主要在 CLI／run plan／报告的 kind 分派，若不新增 kind，需确认「同 kind 不同协议」不会让 capability 校验产生歧义。
- 原始字节收发合同一旦发布即为公共合同，需在 RFC 中按 append-only 方式冻结，避免后续反复改签名。
