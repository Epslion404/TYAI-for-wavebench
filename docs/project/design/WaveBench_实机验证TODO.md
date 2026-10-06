# 实机验证 TODO（安全门与电源保护）

配套规范：[自定义设备与协议支持开发规范](WaveBench_自定义设备与协议支持规范.md) 第 5 条
「实机验收必须单独授权」。

## 为什么有这个文件

2026-10-03 合并的 P0 批次（#40、#60、#42、#76、#64、#65）全部只有**离线证据**：fake transport、
合成数据、单元与回归用例。这些改动触及的却是「失败后 OFF 约束」和「电源输出开启前检查」两条
安全关键路径，离线通过不等于实机验收，也不得在文档里写成已发布能力。

本文件定义实机确认场景与证据要求；执行状态和验收记录维护在对应 issue，汇总入口为
[#57](https://github.com/Epslion404/TYAI-for-wavebench/issues/57)。代码修复合并与实机验收分别记录，
issue 的关闭状态本身不能作为实机验证证据。

## 通用记录要求（每项都要）

按 [WaveBench skill](../../../.agents/skills/wavebench/SKILL.md) 核对当前授权、接线、驱动 capability
和资源租约，记录 IDN、初态、写前快照、回读结果与未恢复项。缺一项视为证据不完整。
真实地址、序列号和原始实验数据只保存在本地；issue 中发布脱敏型号、代码 SHA、验证步骤、
结果与限制。接线或设备能力不足时，在对应 issue 记录具体阻塞。

## 待验证项

- **TUI 持久会话电源输出保护**（状态与证据：[#41](https://github.com/Epslion404/TYAI-for-wavebench/issues/41)；PR #42，merge `354451f`）
  - 场景 A：持久会话里把设定电压推到超过 OVP 阈值，再开输出 → 必须被拒，且拒绝原因可诊断
  - 场景 B：保护已触发（`ovp_tripped` / `ocp_tripped` != `NO`）时尝试输出 ON → 必须被拒
  - 场景 C：同样的两步走 `PowerService.set_output` 路径，行为必须与 TUI 路径一致
  - 难点：需要一台可控触发 OVP/OCP 的电源，先确认触发后能否安全复位

- **安全门已 OFF 后记录写入异常不得按原 ON 快照恢复**（状态与证据：[#32](https://github.com/Epslion404/TYAI-for-wavebench/issues/32)；PR #40，merge `ff2715c`）
  - 场景：让 `write_step_record` 失败，确认 source 保持 OFF
  - 场景：同一条路径下走 KeyboardInterrupt，确认 post-restore 门仍执行且报告的通道集合正确

- **扫频包装异常不得绕过已启用安全门**（状态与证据：[#33](https://github.com/Epslion404/TYAI-for-wavebench/issues/33)；PR #60，merge `fe411af`）
  - 场景：扫频步骤抛非传输类 WaveBenchError 且安全门已启用，确认 source/power 的 OFF 仍执行
  - 场景：缺配置导致的 ConfigError 覆盖一遍

- **多通道推导覆盖全部 channels 与隐式默认目标**（状态与证据：[#34](https://github.com/Epslion404/TYAI-for-wavebench/issues/34)；PR #76 / #64 / #65，merge `64cc852` / `c3e0671` / `62e909b`）
  - 场景：`channels=[1,2]` 的 source 步骤失败后，1 和 2 都收到 `enabled=False`
  - 场景：未显式声明 OFF 清单时，隐式默认 OFF 目标与默认 restore 目标都被覆盖，且去重正确
  - 场景：与显式授权清单共存时，既有语义不变

## 不在此列的

- #36 / PR #62（偶数样本 FFT Nyquist 幅值）：纯数值回归，合成数据即可覆盖，无需实机
- 其余 P1／P2 批次：是否安排真机确认，由各自 issue 的验收口径决定

## 未完成时的表述约束

缺少对应 issue 中绑定代码 SHA 的完整实机证据时，相关能力在文档、报告与对外材料里只能写成
「离线验证通过，实机验收待做」。
