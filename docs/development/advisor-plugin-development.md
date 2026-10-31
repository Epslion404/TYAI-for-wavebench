# advisor 插件开发

本页适用于为 WaveBench 提供 **advisor** 实现的开发者。advisor 接收应用状态与封闭问题集，输出 typed judgment（选项、分数、概率），供操作者参考；它不做仪器 I/O，也不决定任何安全或质量结论。

> 实施状态：`advisor` 类别已在开发线实现（类别与注册表、内置确定性基线、`advisor ask`），**未随正式版本发布**；包内安装与生命周期路径尚未实现（[#19](https://github.com/Epslion404/TYAI-for-wavebench/issues/19) 单独跟踪）。合同状态见 [RFC 索引](../rfcs/index.md)（`Draft`）。

## 类别边界

插件按**类别**分区：`instrument` 与 `advisor` 各有独立的 entry point 组和 API 版本门，能力命名规则互不借用。

- `instrument`：提供 descriptor 与 driver，选中后进入真实仪器执行。
- `advisor`：只产出建议，**不参与写路径、安全门、质量门与 capability 判定**。
- 一个包首版只允许声明一个类别：同时声明 `wavebench.instruments` 与 `wavebench.advisor` 的包会被拒绝。

## 插件契约

`AdvisorPlugin` 定义在 `wavebench.plugins.api`，字段与约束如下。

| 字段 | 约束 |
| --- | --- |
| `advisor_id` | 必填、去空格；必须与 entry point 名一致 |
| `display_name`、`provider` | 必填、去空格 |
| `capabilities` | 非空且不重复；取值来自固定词表（见下） |
| `summary` | 面向人的一句话说明 |
| `egress` | `EgressDeclaration`，声明数据会不会离开本机 |
| `api_version` | 默认 `wavebench.advisor.v1`；其它值在加载期被拒 |
| `package`、`origin` | 由加载路径填写 |

advisor 不要求 `kind`、`models` 或 `manufacturer`。

固定词表由 Core 拥有：`advisor.route`、`advisor.triage`、`advisor.cause`、`advisor.rank`、`advisor.external_state`。出现词表之外的 capability 会在加载期被拒。

## 外发声明

`EgressDeclaration` 只描述事实，**不判断是否已获得授权**——同意门归 Core。

| 字段 | 约束 |
| --- | --- |
| `transmits_off_machine` | 是否会把状态发出本机 |
| `purpose` | 必填、去空格；说明外发目的 |
| `endpoint_hosts` | 外发目标主机；`transmits_off_machine = false` 时必须为空 |
| `allowed_state_fields` | 允许出现在外发状态里的字段名；传输型必须非空 |

传输型 advisor 必须同时声明 `advisor.external_state` capability；非传输型不得声明它。

```python
from wavebench.plugins.api import AdvisorPlugin, EgressDeclaration

plugin = AdvisorPlugin(
    advisor_id="example.jev_router",
    display_name="Example Jev router",
    provider="example",
    capabilities=("advisor.route", "advisor.external_state"),
    summary="Route a request to one WaveBench entry point.",
    egress=EgressDeclaration(
        transmits_off_machine=True,
        purpose="Send the whitelisted run state to the configured Jev endpoint.",
        endpoint_hosts=("api.example.com",),
        allowed_state_fields=("request", "run_status"),
    ),
)
```

本地确定性实现把 `transmits_off_machine` 设为 `false`，并省略两个白名单字段。

## entry point 与加载

- 组名：`wavebench.advisor`；加载对象必须是 `AdvisorPlugin`，或其可调用结果。
- entry point 名必须等于 `advisor_id`，否则记 `PluginLoadError`，不进入注册表。
- 第三方 entry point 默认不导入：`plugin list --category advisor --include-entry-points` 才加载。
- 加载失败只记 doctor 记录，不抛异常；`plugin doctor --category advisor` 会输出 `error` 行。

```bash
python -m wavebench plugin list --category advisor
python -m wavebench plugin doctor --category advisor --include-entry-points
```

`--category` 默认 `instrument`；advisor 类别下 `--kind` 与 `--load` 会报错，因为它们只对仪器插件有意义。

## Core 拥有的部分

以下环节由 Core 实现，插件不得自己实现或绕过：

| 环节 | 由谁实现 |
| --- | --- |
| 外发同意门（默认关闭、显式 opt-in、非交互拒绝） | `wavebench.services.advisor_consent` |
| 发送前预览（脱敏、字节数、sha256、逐条不可信来源） | 同上；预览不联网，且**预览内容就是实际外发内容** |
| 「概率 → 动作」阈值 | `[advisor] accept`／`review`，插件不得自带阈值 |
| decision artifact 落盘 | `wavebench.services.decision_artifacts`，schema `wavebench.decision.v1` |
| 拒绝与失败语义 | 见下表 |

| 情形 | 结果 |
| --- | --- |
| 未开启、未同意、非交互、字段或 endpoint 未注册 | `refused`，**不发起任何外发** |
| 仅预览 | `preview_only`，不发起外发 |
| 没有可用实现或调用失败 | `unavailable` |
| 响应结构非法或选项 id 不在代码持有的选项集内 | `invalid`，不采用任何结论 |

以上四种都不改变退出码，也不参与 `run.json.status`、质量门、`auto_recover` 或 capability 判定。

## 实现流程

1. 选定 `advisor_id` 与 capability（必须在词表内），并写清 egress 事实。
2. 实现与 Core 约定的调用形状：

   ```python
   def system_one(self, *, model: str, state: Mapping[str, Any], questions: Mapping[str, Any]) -> Mapping[str, Any]: ...
   ```

   建议直接对照内置确定性基线 `builtin.rule_advisor`（`wavebench.services.advisor_rule`）：它不联网、不依赖 SDK，用来回答「外部 advisor 是否真的打败了确定性基线」。
3. 选项集与答案契约由 Core 与调用方持有，模型不得产出命令文本；建议里的命令一律来自代码持有的 catalog。
4. 离线测试至少覆盖：拒绝路径下外部调用次数为 0、响应非法时 fail-closed、相同输入产生相同字节、以及不写 Core 拥有的产物。

## 验证

```bash
python -m wavebench plugin list --category advisor
python -m wavebench plugin doctor --category advisor
python -m wavebench advisor ask --state state.json --config wavebench.toml --preview
```

`advisor ask` 读取本地 state fixture 与 `[advisor]` 配置，默认只打印脱敏预览，不发起外发；`--accept-external-state` 才可能在通过同意门后外发，`--run-dir` 决定 decision artifact 的落点。

## 兼容策略

- `advisor` 使用独立版本门 `wavebench.advisor.v1`；调整契约时提升该门，不动 `wavebench.instrument.*`。
- 现有仪器插件契约、entry point 组与账本记录不受 advisor 类别影响；`plugin list` 不带 `--category` 时行为与输出保持仪器语义。
- 包内安装、依赖校验与漂移检测尚未覆盖 advisor 包，因此本阶段的 advisor 实现以开发线加载与内置基线验证为准，不能据此声明生产支持。

## 相关页面

- [插件模型](../concepts/plugin-model.md)
- [插件 Reference](../reference/plugins/index.md)
- [配置 Reference](../reference/configuration.md)
- [RFC 索引](../rfcs/index.md)
