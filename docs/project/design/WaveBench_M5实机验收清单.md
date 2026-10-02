# M5 实机验收清单（设备线与 Jev 线共用）

本页固定 M5「验证与交付」的实机验收口径，两条扩展支线共用。**默认不动硬件**：只有在本页清单被逐项确认、且操作者已明确授权后，才执行会写入仪器或触发采集的步骤。

## 使用方式

1. 先跑完「一、离线」全部项；离线不通过不得进入实机阶段。
2. 逐项记录命令、期望、实测与证据路径；不记录「应该可以」这类判断。
3. 任一阶段失败即停止，保留现场与产物，不要跳到下一阶段。
4. 未取得授权的写入／触发步骤一律留空并注明「未授权」。

## 一、离线（无硬件、无 API key）

| 项 | 命令 | 通过判据 |
| --- | --- | --- |
| 全量测试 | `python -m pytest -q` | 无非预期失败（本机已知基线失败需单独注明） |
| 静态检查 | `python -m ruff check .` | 无输出 |
| 生成文档同步 | `python scripts/generate_docs.py --check` | `generated Reference is current` |
| 文档审计 | `python .agents/skills/wavebench-docs/scripts/audit_docs.py --quiet-warnings` | 0 errors |
| 作品包测试 | `cd 比赛区/天翼AI电子科研Skill && python -m pytest -q` | 全部通过 |
| 快照一致性 | `python scripts/check_snapshot_drift.py` | `IN SYNC` |
| 离线演示 | `python -m tianyi_electronics_skill analyze --demo` | 输出波形指标，无网络与设备访问 |
| 离线决策 | `python -m tianyi_electronics_skill advise --request "检查已有的离线波形"` | `external_call_count = 0`、`may_write_device = false` |

## 二、只读连接（读取，不改变设置）

| 项 | 命令／方式 | 通过判据 |
| --- | --- | --- |
| 配置可解析 | `python -m wavebench doctor --config wavebench.toml` | 目标仪器身份与预期一致 |
| 身份读取 | `*IDN?`（经 driver 或 `doctor`） | 与 descriptor 的 `idn_patterns` 匹配 |
| 错误队列 | 读取设备错误队列 | 空或已知旧错误，无新增 |
| 通道／状态 | 各驱动声明的状态查询 | 只读，且返回可解析 |
| 资源租约 | `python -m wavebench lock status <resource>` | 无其他持有者；查询不取得锁 |

注意：`doctor` 与 `run verify` 会访问真实仪器，属于读取路径；执行前确认资源、接线、输入阻抗与输出状态。

## 三、写前预检（仍不写入）

| 项 | 命令／方式 | 通过判据 |
| --- | --- | --- |
| plan 校验 | `python -m wavebench run check --plan <plan>` | 不连接仪器即通过 |
| plan 预检 | `python -m wavebench run verify --config wavebench.toml --plan <plan>` | 所需仪器、能力与安全前提齐备 |
| access policy | 核对 `[scope]`／`[source]`／`[power]`／`[dmm]` 的 `access` | 写入步骤所需权限与配置一致 |
| 恢复能力 | 源驱动的基础恢复接口（`snapshot_basic_state`） | 快照可读且声明可恢复；未声明时注明未覆盖项 |
| 安全上限 | `[safety_limits]` 与终结设置 | 未被放宽；无 `--allow-50ohm` 之类临时开关 |

## 四、受控写入（需明确授权，逐项执行）

| 步骤 | 前置 | 写后必须做的事 |
| --- | --- | --- |
| setter（频率／幅度／功能） | 输出 OFF、量程与终结确认 | 回读并比较关键字段 |
| 任意波上传 | 目标通道与幅度预算 | 回读校验；记录覆盖范围 |
| 输出切换 ON | 操作者确认负载与幅度 | 记录时间与读数，准备立即关闭 |
| 采集（单次／平均） | 触发与超时设置确认 | 校验波形与质量门 |
| 采集（扫频／频响） | 频段、点数、预算确认 | 记录进度与中断恢复点 |

红线：不自动执行 `*RST`；不因设置电压／幅度／频率而自动打开输出；不静默放宽安全上限；不把建议直接当作命令执行。

## 五、收尾与恢复

| 项 | 通过判据 |
| --- | --- |
| 输出状态 | 明确记录实际状态；未经确认不得假定 OFF |
| 基础恢复 | 按快照恢复并回读比较；未覆盖状态列入产物 |
| 错误队列 | 复读一次并记录 |
| 会话健康 | 无 poisoned／unknown 残留；若 poisoned，保留现场并说明 |
| 产物 | `run.json`、`summary.csv`、`steps/`、`decisions/`（如启用 advisor）路径齐全 |
| 现场 | 接线、仪器面板与初始记录一致 |

## 记录模板

| 阶段 | 项 | 命令 | 期望 | 实测 | 证据路径 | 结论 |
| --- | --- | --- | --- | --- | --- | --- |
| 一 | 全量测试 | `python -m pytest -q` | 无非预期失败 | | | |
| 二 | 身份读取 | `doctor` | 与 descriptor 匹配 | | | |
| 四 | 输出 ON | 明确授权 | 记录状态并立即准备关闭 | | | |
| 五 | 基础恢复 | 回读比较 | 覆盖字段一致 | | | |

## 相关页面

- [自定义设备与协议支持 TODO](WaveBench_自定义设备与协议支持TODO.md)：M5 的验收与交付项
- [Jev 轻量化自动决策 TODO](WaveBench_Jev轻量化自动决策TODO.md)：J4 的作品包接入与交付
- [仓库协作与分支约定](仓库协作与分支约定.md)：Release 与打包规则
