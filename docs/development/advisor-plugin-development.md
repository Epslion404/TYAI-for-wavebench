# advisor 插件开发

本页适用于为 WaveBench 提供 **advisor** 实现的开发者。advisor 接收应用状态与封闭问题集，输出 typed judgment（选项、分数、概率），供操作者参考；它不做仪器 I/O，也不决定任何安全或质量结论。

> 实施状态：`advisor` 类别已在开发线实现（类别与注册表、内置确定性基线、`advisor ask`，以及包内安装与生命周期，[#19](https://github.com/Epslion404/TYAI-for-wavebench/issues/19)），**未随正式版本发布**。合同与裁决见 [advisor 插件类别 RFC](../project/rfcs/WaveBench_advisor插件RFC.md)（`Draft`）。

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
- 注册前校验 API 版本、capability 词表与外发声明；不符合合同的插件记入加载错误。
- 第三方 entry point 默认不导入：`plugin list --category advisor --include-entry-points` 才加载。
- 加载失败只记 doctor 记录，不抛异常；`plugin doctor --category advisor` 会输出 `error` 行。

```bash
python -m wavebench plugin list --category advisor
python -m wavebench plugin doctor --category advisor --include-entry-points
```

`--category` 默认 `instrument`；advisor 类别下 `--kind` 与 `--load` 会报错，因为它们只对仪器插件有意义。

## 打包与安装

advisor 包与仪器插件共用受管生命周期：`plugin package check`、`install`、`installed`、`upgrade`、`downgrade`、`remove`、`recover`。类别由包声明的 entry point 组决定，不需要额外参数；同时声明 `wavebench.instruments` 与 `wavebench.advisor` 的包会被拒绝。

- 安装保持离线 `--no-deps`，不会联网下载 advisor 的运行时依赖。
- postflight 按类别调用 `validate_advisor`；非法 API 版本、capability 或 egress 声明会让安装失败并回滚。
- `plugin doctor --category advisor` 从打包元数据 `Requires-Dist` 读取依赖，报告缺失或版本不匹配，不联网。

```bash
python -m wavebench plugin package check dist/wavebench_example_advisor-0.1.0-py3-none-any.whl
python -m wavebench plugin install dist/wavebench_example_advisor-0.1.0-py3-none-any.whl
python -m wavebench plugin doctor --category advisor --include-entry-points
```

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

响应中的概率、非空 `confidence` 与 `noul` 必须是 0 到 1 之间的有限数字，布尔值和数字字符串会被拒绝；`score` 必须是有限数字。`confidence` 缺失或为 `null` 表示未提供。Choice 必须提供字符串选项 id 及其概率，路由概率表中的选项也必须属于代码持有的 catalog。非法响应产出 `invalid` artifact，建议集为空。

Decision artifact 的 `reason`、requested model 与 reported model 在构造时统一采用同意门的敏感文本脱敏规则，CLI 诊断与 JSON 落盘读取同一份结果。未知选项和答案类型的错误只保存固定诊断，不回显响应原文。

### 敏感文本与编码 JSON 的边界

Python mapping 中的敏感键（忽略大小写，`-` 归一为 `_`，包含 `api_key`、`apikey`、`authorization`、`credential`、`password`、`secret` 或 `token`）对应的**整个值**替换为 `[REDACTED]`，不解析该值。普通文本继续识别 `token=...` 等标签、带引号的 Bearer 值和转义引号；未闭合的值只遮蔽本行，无引号的值以空白为界，逗号或花括号不会结束凭据。

完整 JSON 文本（去掉前导 JSON 空白后以 `"`、`{` 或 `[` 开始）在预算内通过标准 JSON 解码、敏感键脱敏再编码；例如 key 的 `\u0061uthorization` 会按 `authorization` 处理。每一层 JSON string 的类型与编码层数保持不变，输入 `str` 不会变成 Python dict。预算内没有脱敏变化的合法 JSON 保留原始字节；发生变化的 JSON 用标准 `json.dumps(..., ensure_ascii=True)` 重编码，所以空白和 Unicode 转义形式可能改变，非敏感字段的内容保留。

解码后的 JSON key 本身也是文本，使用同一共享预算与脱敏规则；例如 `{"token=dummy": "private"}` 的字段名和值都会变为 `[REDACTED]`。是否遮蔽整个值仍由原字段名判定。若两个不同 key 脱敏后成为同名，整个原始字符串替换为 `[REDACTED_ENCODED_TEXT]`，不选择其中一个值或静默丢弃字段；同字符串中的普通诊断也会被省略。

开头的完整 JSON string token 后还可以追加普通诊断，例如 `json.dumps(json.dumps({"token": "dummy"})) + " diagnosis=keep"`：先按转义引号边界识别 token，解码并脱敏后重编码，再独立脱敏并保留后缀。token 与后缀共享原字符串的预算，候选输入的字节统计包含后缀；任一部分超限仍省略整个字符串。

每个待处理字符串单独拥有以下解释预算，包含其解码出的嵌套字符串，共享而不在子节点重置：

| 限制 | 上限 |
| --- | --- |
| 每次 JSON 解码的输入 | 65,536 UTF-8 字节（孤立 surrogate 按 `surrogatepass` 计数） |
| 重编码 JSON 输出（包含普通后缀） | 65,536 UTF-8 字节；按有界 JSON 树的编码 chunk 计数，超限前不拼接完整输出 |
| 每轮累计待解码文本工作量 | 262,144 字节 |
| 每轮 JSON 解码尝试次数（包括失败尝试） | 8；小对象最多支持 7 层外部 JSON string 包装加一次对象解码 |
| 结构深度 | 32；同时限制解码树遍历与当前路径上的容器/编码字符串 |
| 每轮累计词法 token 数 | 4,096；容器开头、key/value 字符串和原子值均计数，字符串内的标点不计数 |

字节、次数、容器深度和 token 检查发生在 `json.loads` **之前**，不先分配无限制 JSON 树再统计。重编码只遍历已经有界的 JSON 树；单个字符串编码 chunk 可能展开有界 scalar，但不会拼接超过输出预算的文档。改变后的结果还必须通过**一次**同样有界的稳定性校验，确认下一次脱敏会原样接受它；校验不是反复重置预算直到收敛的循环。每个原始字符串最多两轮，共 16 次 JSON 解码尝试、524,288 字节解码工作量、8,192 个词法 token。原样返回的输入和固定 marker 不需要第二轮。这样即使输出低于单次字节上限，但多层解码累计工作量超限，也会在首轮调用返回 marker，保持 preview、client 和 artifact 再次脱敏的一致性。

任一输入、输出或稳定性校验预算耗尽（或复验结果发生变化），整个原始字符串替换为 `[REDACTED_JSON_LIMIT]`，不会回退到旧文本正则或保留已处理前缀/未处理后缀；此时该字符串中的诊断也会丢失。超过预算的 JSON 候选即使没有凭据也会被省略。普通非 JSON 长诊断没有该长度截断，继续进行与输入长度成比例的文本匹配。

`[REDACTED]`、`[REDACTED_JSON_LIMIT]`、`[REDACTED_ENCODED_TEXT]`、`[REDACTED_UNTRUSTED_TEXT]` 四个固定 marker 原样保留，不花费 JSON 解码次数。已知 marker token 后接空白与普通后缀（例如 `[REDACTED] diagnosis=keep`）按普通文本规则处理，后缀中的凭据仍需脱敏；不会因为数组中有多个这样的脱敏值而在下一轮多消耗 JSON 尝试。其它以 `[` 开头的字符串和 marker 相似文本仍使用原来的候选与保守规则，不能借用 marker 前缀跳过凭据检查。

除上述开头 token 加后缀外，自由文本中嵌入的转义 JSON 片段不提供通用还原保证。无法解析的完整 JSON string 候选、或回退文本按普通标签规则脱敏后仍出现「反斜杠转义引号 + 空白 + 冒号」/`\uXXXX` 标记时，整个字符串保守替换为 `[REDACTED_ENCODED_TEXT]`，同样会丢失其中的普通诊断。没有这些编码标记的普通文本、单引号字段和非完整普通 JSON 继续使用上述按行/引号规则。插件应优先用结构化 mapping 表达凭据，不能将这些有限规则解释为任意编码、自由文本或任意 payload 的安全保证。

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

自动化调用使用 `advisor ask --json`，输出沿用 `wavebench.cli.result.v1` 包络。
`result` 为结构化对象，包含 `status`、`reason`、`requested_model`、`reported_model`、
`duration_ms`、`thresholds`、`consent`、`recommendations`、`artifact_path`，以及
`target`、`state`、`questions`、`answers` 和 `advisory_only`。
`consent` 保留 `payload_sha256`、`payload_bytes` 和 `accepted_data_leaves_machine`；
无产物写入时 `artifact_path` 为 `null`，无建议时 `recommendations` 为 `[]`。
`preview_only`、`refused`、`unavailable` 和 `invalid` 同样使用该形状。
包络的 `status`／`exit_code` 表示 CLI 执行结果；advisor 的判断状态位于 `result.status`。
有效调用得到拒绝或无可用判断时，既有 CLI 退出码仍为 `0`；配置或参数错误仍使用既有错误包络。
不带 `--json` 的文本输出保持原样；落盘产物仍使用 `wavebench.decision.v1`，同意门与产物行为不变。

## 兼容策略

- `advisor` 使用独立版本门 `wavebench.advisor.v1`；调整契约时提升该门，不动 `wavebench.instrument.*`。
- 仪器插件的 entry point 组、安装路径与 CLI 输出保持不变；账本记录新增可选的 `category` 字段，旧记录按 `instrument` 读取。`plugin list` 不带 `--category` 时行为与输出保持仪器语义。
- 包内安装、postflight（`validate_advisor`）、账本/漂移检测与依赖 doctor 已覆盖 advisor 包；仍应逐包验证真实 advisor 的运行时依赖与外部服务，不能据此声明所有第三方 advisor 的生产支持。

## 相关页面

- [插件模型](../concepts/plugin-model.md)
- [插件 Reference](../reference/plugins/index.md)
- [配置 Reference](../reference/configuration.md)
- [advisor 插件类别 RFC](../project/rfcs/WaveBench_advisor插件RFC.md)
