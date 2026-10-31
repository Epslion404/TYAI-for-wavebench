# CLI Reference

本页说明 WaveBench CLI 的入口、输出和副作用边界。完整参数以当前安装版本的 `python -m wavebench --help` 及各子命令 `--help` 为准。

## Synopsis

```bash
python -m wavebench --help
python -m wavebench run --help
python -m wavebench --json <domain> <command> ...
```

一级命令域包括 `scope`、`source`、`rf-source`、`power`、`dmm`、`sweep`、`run`、`capture`、`mcp`、`tui`、`net`、`doctor`、`plugin`、`advisor`、`capability` 和 `lock`。`run` 的当前子命令由 `python -m wavebench run --help` 输出。

## 副作用

| 类别 | 命令族 | 行为 |
| --- | --- | --- |
| 离线、只读本地文件 | `run schema`、`run check`、`run intent`、`run compare`、`run resume`、`capture inspect`、`capability explain`、`lock status`、`plugin protocol check/info` | 不连接仪器。 |
| 离线且可能写本地文件 | `run template --output`、`run report`、`run report-index` | 不连接仪器，但会创建模板、报告或索引文件。 |
| 连接读取或预检 | `doctor`、状态／身份查询、`run verify` | 会访问配置的仪器，不应改变实验设置。 |
| 可能改变状态或触发采集 | 输出和 setter、`scope auto`／`scope capture`、非 fake TUI、`run plan` | 可能写入仪器、触发采集或切换输出。 |

`scope fetch` 读取已有波形，但仍是仪器 I/O；不要把它当作离线命令。每次硬件操作前确认接线、输入阻抗、输出状态和安全限制。WaveBench 不会自动执行 `*RST`，也不会因设置电压、幅度或频率而自动开启输出。

## net discover

`net discover --subnet <CIDR>` 是只读的局域网扫描：探测 TCP 端口、对已知 SCPI 端口发送只读
`*IDN?`，并把 VXI-11 候选、Modbus/TCP（502）与未知端口分开标注，非 SCPI 端口只提示、不当作
仪器。默认用 `--max-hosts` 限制单次扫描主机数；`--force` 可跳过该确认阈值。

本地设备枚举通过附加选项加入同一输出：
- `--serials`：枚举本机串口（pyserial `list_ports`），`source=serial`、`protocol=serial`；
  无 pyserial 或无权限时给结构化错误，而不是空列表。
- `--can`：枚举 python-can 检测到的真实适配器配置（`source=can`、`protocol=can`，资源形如 `can://<interface>/<channel>`）；
  需要 `pip install wavebench[can]`。无适配器或检测失败时输出 `status=none` 的结构化结果，而不是误导性列表。
- `--summary`：追加一行按 source 与 protocol 聚合的摘要。
- `--json`：结果以 `wavebench.cli.result.v1` 输出，字段与表头一致。

## advisor 决策入口

`advisor ask` 是离线决策入口：它读取本地 state fixture 与 `[advisor]` 配置，默认只打印脱敏后的预览，不发起任何外发；只有显式 `--accept-external-state` 且通过同意门（endpoint 与字段白名单）时才可能把状态发出本机，`--run-dir` 指定 decision artifact 的落点。它不连接仪器，也不参与写路径、安全门、质量门或 `auto_recover`。

实现状态：开发线已实现，未随正式版本发布。`advisor` 包的生命周期（`plugin package check` / `install` / `installed` / `remove` / `upgrade` / `downgrade` / `recover`）由插件域管理，类别由 `wavebench.advisor` entry point 推断；用法见[管理插件](../how-to/manage-plugins.md)。合同见 [advisor 插件类别 RFC](../project/rfcs/WaveBench_advisor插件RFC.md)，实现流程见[advisor 插件开发](../development/advisor-plugin-development.md)。

## JSON 输出与退出码

将 `--json` 放在命令行任意位置可请求机器可读输出。成功结果使用 `wavebench.cli.result.v1`，包含 `status`、`exit_code` 和 `result`；错误使用 `wavebench.error.v1`。普通成功输出写入标准输出，普通错误写入标准错误。

错误分类和稳定退出码见[错误 Reference](errors.md)。`run plan` 即使保留了运行产物，只要存在失败 step 也会返回非零状态。

## 相关页面

- [无硬件快速开始](../getting-started/quickstart.md)
- [配置实验台](../getting-started/configure-bench.md)
- [执行一次实验](../how-to/run-an-experiment.md)
- [run plan Reference](run-schema.md)
- [插件 Reference](plugins/index.md)
