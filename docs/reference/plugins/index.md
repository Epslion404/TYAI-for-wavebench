# 插件 Reference

WaveBench 支持下列彼此独立的插件路径。它们的共同边界是：Core 拥有 resource、transport、Service、安全、run plan、artifact 和 session/recovery；插件实现厂商协议、命令、解析、descriptor 和私有测试。

| 路径 | 入口 | 是否进入真实执行 |
| --- | --- | --- |
| V1 metadata | `wavebench.drivers` / `wavebench.instrument.v1` | 否。用于展示 metadata。 |
| V2 executable plugin | `wavebench.instruments` / `wavebench.instrument.v2` | 仅在配置选中 driver 后。 |
| 声明式 SCPI TOML | 本地 TOML | 否；只允许显式只读 IDN probe。 |
| 声明式二进制帧 TOML | `plugin protocol check/info` | 否；离线校验描述和单帧。 |
| advisor 类别 | `wavebench.advisor` / `wavebench.advisor.v1` | 否；只产出建议，不连接仪器。 |

## 加载与信任边界

默认插件命令优先查看内建 metadata。`plugin ... --load` 会导入第三方 V2 descriptor；只在可信环境中使用。Service 仅在实际打开已配置的 driver 时加载对应插件，未选中的坏插件不应阻断其它内建路径。

V2 entry point 名必须与 canonical `driver_id` 一致。可执行插件不能通过 alias 覆盖内建 driver；具体分发包、型号支持和 capability 以 descriptor 与插件仓库为准。

## 声明式 SCPI 与本地市场索引

`plugin scpi check`、`plugin scpi doctor` 和 `plugin scpi info` 默认只读取 TOML。只有显式 `--probe --resource` 或 `plugin scpi probe` 才会发送配置的单行 IDN 查询；该路径不提供任意 SCPI 或写入。

`plugin market` 读取本地 JSON 索引，用于搜索和查看条目。它不下载、安装、导入或执行索引中声明的包。

字段、默认值和 JSON／TOML 校验规则由 parser 与 validator 定义。不要把本页的概览当作完整 schema。

## 声明式二进制帧

`plugin protocol check` 和 `plugin protocol info` 通过
位置参数 `path` 读取本地 TOML，不打开 transport。字段与默认值由
`src/wavebench/plugins/protocol.py` 定义；manifest 上限为 1 MiB，帧与 header 上限为 64 MiB。
插件 metadata 和必填帧字段必须完整，非法描述统一返回配置错误。

支持定长、长度字段和分隔符三种 framing，以及可选 CRC16/Modbus。单帧校验要求完整
header 和精确长度；分隔符 framing 以首次分隔符结束，拒绝内部提前分隔符和拼接帧。
`escape_hex` 当前没有约定字节填充编码，声明后会报错。帧校验返回原始字节；请求映射、
采样解码、流拆帧与真实驱动执行由后续协议合同定义。

## advisor 类别

`advisor` 是第二类插件：接收应用状态与封闭问题集，输出 typed judgment。它不连接仪器，也不参与写路径、安全门、质量门与 capability 判定；数据外发同意门与 `wavebench.decision.v1` 产物由 Core 拥有，插件只声明 egress 事实。

`plugin list` 与 `plugin doctor` 用 `--category {instrument|advisor}` 选择类别（默认 `instrument`）。`--include-entry-points` 在 advisor 类别下加载 `wavebench.advisor` 入口点；`--kind` 与 `--load` 只对仪器插件有效，advisor 类别下会报错。

实现状态：开发线已实现，未随正式版本发布；包内安装与生命周期路径尚未覆盖。合同与裁决见 [advisor 插件类别 RFC](../../project/rfcs/WaveBench_advisor插件RFC.md)，实现流程见[advisor 插件开发](../../development/advisor-plugin-development.md)。

## 相关页面

- [管理仪器插件](../../how-to/manage-plugins.md)
- [插件开发](../../development/plugin-development.md)
- [advisor 插件开发](../../development/advisor-plugin-development.md)
- [新增仪器驱动](../../development/instrument-drivers.md)
- [仪器插件仓库](https://github.com/Scaxlibur/wavebench-instrument-plugins)
