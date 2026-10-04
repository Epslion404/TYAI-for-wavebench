# 离线示例

这些示例不需要仪器、网络或 API Key，用来演示 Skill 在没有硬件时能做什么，以及两个示例数据文件的用途。

## 数据文件

| 文件 | 用途 |
| --- | --- |
| `demo_capture.json` | 一份 24 点、8 kHz 的示例采集包（`sample_rate_hz` + `samples`），供 `analyze`／`plot` 离线读取 |
| `protocols/vendor_can_f32_le.json` | 声明式协议描述示例：CAN 传输、4 字节头、小端 float32 载荷。用来演示「帧定义留在描述文件与插件，不进 Core」 |

## 三条离线命令

以下命令在本目录下执行，输出即为离线路径的预期结果。

```powershell
# 1) 内置演示数据
python -m tianyi_electronics_skill analyze --demo

# 2) 读取本目录的示例采集包
python -m tianyi_electronics_skill analyze --file examples/demo_capture.json

# 3) 离线决策建议：只用内置确定性基线
python -m tianyi_electronics_skill advise --request "检查已有的离线波形"
```

预期：

- 前两条输出峰峰值、频率与 RMS 等指标，并用 `source` 标明数据来源（`built-in-demo` 或文件路径）；
- 第三条输出 `mode = offline_rule_baseline`、`external_call_count = 0`、`may_write_device = false`，建议里的 `command_template` 只含占位符，不会生成可执行写入命令。

## 边界

- 示例只覆盖离线路径：不发现、不连接、不写入设备。
- 真实设备流程见 [README](../README.md) 与 [SKILL.md](../SKILL.md)。
- 实机验收需单独授权，清单见仓库的 `docs/project/design/WaveBench_M5实机验收清单.md`。
