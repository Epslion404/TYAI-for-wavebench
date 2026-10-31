---
name: tianyi-electronics-research
description: >-
  面向电子科研的 TeleAgent Skill：发现局域网 SCPI 仪器，只读检查、测量并采集示波器波形，
  离线分析后输出可追溯的 JSON/SVG；真实设备写入必须经用户明确授权。
license: MIT
metadata:
  version: 1.0.0
  project: 天翼AI杯-重庆邮电大学赛区
  compatibility: Python 3.11+；Windows/Linux；真实设备操作需要用户确认网口、接线和写入权限。
---

# 天翼 AI 电子科研赋能 Skill

这个 Skill 把 WaveBench 的仪器发现、SCPI/VISA 通信和信号分析能力封装成 TeleAgent
可调用的本地工具。默认只读；写设备是显式例外。

## 先按任务选择参考文档

不要一次加载全部参考文档。只读当前任务需要的那一份：

- 命令参数、JSON 字段、退出码、离线行为 → [references/commands.md](references/commands.md)
- 真实设备授权、写入边界、错误分类与恢复 → [references/safety-and-errors.md](references/safety-and-errors.md)
- 协议识别、CAN、跨主机 bridge → [references/protocols-and-bridges.md](references/protocols-and-bridges.md)
- 波形格式、measure/fetch/analyze 指标、SVG → [references/waveforms.md](references/waveforms.md)
- 连接失败、空响应、部分成功、能否重试 → [references/troubleshooting.md](references/troubleshooting.md)

## 可调用能力

- `discover`：扫描指定网段的常见仪器端口，尝试 `*IDN?`，输出 IP、端口、协议标签和设备标识。
- `inspect`：只读查询设备 ID、`*OPC?`、错误队列和示波器通道摘要。
- `measure`：读取频率、Vpp、Vmax、Vmin、Vavg、Vrms；全部标量都不支持时可降级为读波形。
- `fetch`：读取原始波形（REAL 二进制块或 ASCII），输出时间轴、一致性检查、离线指标和 CSV/JSON。
- `analyze`：对 CSV/JSON 波形离线计算峰值、峰峰值、频率、RMS、占空比、上升时间、过冲、DFT/THD。
- `plot`：生成不依赖第三方绘图库的 SVG 单帧图。
- `advise`：使用内置确定性规则基线，在 `discover`、`inspect`、`analyze` 中建议下一步；不联网、不读设备、不生成写入命令。
- `autoscale`：探测厂商 autoset 或计算等效设置；只有显式 `--apply` 才写设备。
- `protocol identify/decode`：在本地识别/解析 JSON、CAN 或厂商二进制 manifest。
- `bridge serve/expose/connect`：通过可达中继建立反向 TCP 路径。

完整参数见 [references/commands.md](references/commands.md)；协议和 bridge 的边界见
[references/protocols-and-bridges.md](references/protocols-and-bridges.md)。

## 最小工作流

1. 先 `discover`，或让用户提供 `TCPIP::<ip>::INSTR`；不要猜设备地址。
2. 用 `inspect` 做只读确认，记录 IDN、错误队列和通道状态。
3. 解释测量意图后再调用 `measure`；默认只读，不发送 `*RST`，不自动打开输出。
4. 需要原始波形时用 `fetch` 保存 CSV/JSON，再用 `analyze` 做工程解释。
5. 报告写明单位、采样率、数据来源、时间戳，以及未支持/未验证项。

真实设备操作前必须确认接线、输入阻抗、量程和电压/电流上限。连接失败时保持只读诊断，
不要盲目重试或修改设备状态。

## 真实设备边界

- 默认只读；唯一的写入入口是 `autoscale --apply`，并且必须先获得用户对地址、通道和写入范围的明确确认。
- `autoscale --apply` 的每个设置都要回读验证；部分成功退出码为 `3`，未验证任何设置为 `2`。
- 超时、不完整响应或错误队列不可读时，把结果标为未验证；不要自动重放写命令。
- 详细安全规则、错误类别和恢复步骤见 [references/safety-and-errors.md](references/safety-and-errors.md)。

## 入口

在本目录执行：

```powershell
python -m tianyi_electronics_skill discover --subnet 192.168.1.0/24
python -m tianyi_electronics_skill inspect --address 192.168.1.20 --port 5025
python -m tianyi_electronics_skill measure --address 192.168.1.20 --port 5025 --channel CHAN1
python -m tianyi_electronics_skill fetch --address 192.168.1.20 --port 5025 --channel CHAN1 --output wave.csv
python -m tianyi_electronics_skill analyze --demo
python -m tianyi_electronics_skill advise --request "检查已有的离线波形"
```

完整 WaveBench 源码、驱动和示例计划位于 `wavebench/`，可用于高级 run plan、报告和仪器插件。

## Competition extensions

- `autoscale` 先探测厂商命令，只有显式 `--apply` 才写入。
- `discover` 支持协议标签、超时/并发、4096 个 IPv4 目标的安全门、可选 mDNS 和非 SCPI 端口提示。
- `plot` 与 `fetch --plot-output` 生成转义后的 SVG 单帧图；分析包含迟滞频率、上升时间、过冲、DFT 和 THD。
- `protocol identify/decode` 选择 JSON、CAN 或声明式厂商二进制 manifest；Arm Linux SocketCAN 通过可选 `python-can` 使用。
- `bridge serve/expose/connect` 在设备主机与 Agent 主机不在同一局域网时提供反向 TCP 路径；仍需可达中继、VPN 或 SSH 隧道。

## 环境

- Python 3.11+；Windows/Linux。
- 基础安装无第三方依赖；VISA 需要 `.[visa]`，NumPy 可选 `.[numpy]`，CAN 需要 `.[can]`。
- 真实设备操作需要用户确认网口、接线和写入权限。
