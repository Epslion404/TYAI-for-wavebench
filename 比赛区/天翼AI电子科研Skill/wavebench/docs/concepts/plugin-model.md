# 插件模型

WaveBench 支持 metadata、可执行插件和声明式 SCPI 三条插件路径。它们的信任边界与执行能力不同，不应视为同一类扩展机制。

## 三条路径

| 路径 | 用途 | 边界 |
| --- | --- | --- |
| V1 metadata | 展示已有仪器 metadata | 不进入真实仪器执行。 |
| V2 executable plugin | 提供 descriptor 和 driver | 只在配置选中该 driver 时进入执行；第三方代码属于可信本地代码。 |
| 声明式 SCPI TOML | 检查和描述只读 IDN probe | 不提供任意 SCPI 或写入。 |

## 插件类别

路径之上还有一层**类别**：`instrument` 与 `advisor` 各自拥有独立的 entry point 组、API 版本门和能力命名规则，不共用同一个 kind 列表。

| 类别 | entry point 组 | API 版本门 | 是否进入真实执行 |
| --- | --- | --- | --- |
| instrument | `wavebench.instruments`（V2 可执行）、`wavebench.drivers`（V1 metadata） | `wavebench.instrument.v2`、`wavebench.instrument.v1` | 配置选中 driver 后进入。 |
| advisor | `wavebench.advisor` | `wavebench.advisor.v1` | 否；只产出建议产物。 |

advisor 的红线是：不参与任何写路径、安全门、质量门与 capability 判定。数据外发同意门与 decision artifact 由 Core 拥有，插件只能声明「会不会外发」这一事实。一个包首版只允许声明一个类别。

实现状态：advisor 类别已在开发线实现，未随正式版本发布；包内安装与生命周期路径已覆盖。

## 责任分配

Core 负责插件发现、resource、transport、Service、安全、run plan、artifact 和会话语义。插件负责厂商协议、解析、descriptor、型号限制和私有测试。型号级 SCPI、quirk、profile 与实机 evidence 应留在 `wavebench-instrument-plugins`，不要复制到 Core 的通用 Guide。

## 相关页面

- [插件 Reference](../reference/plugins/index.md)
- [管理仪器插件](../how-to/manage-plugins.md)
- [插件开发](../development/plugin-development.md)
- [advisor 插件开发](../development/advisor-plugin-development.md)
- [新增仪器驱动](../development/instrument-drivers.md)
