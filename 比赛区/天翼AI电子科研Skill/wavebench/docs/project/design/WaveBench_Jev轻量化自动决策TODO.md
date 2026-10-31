# Jev 轻量化自动决策 TODO

第二条扩展支线（与[自定义设备与协议支持](WaveBench_自定义设备与协议支持TODO.md)并列）。契约见 [advisor 插件类别 RFC](../rfcs/WaveBench_advisor插件RFC.md)，设计与阶段记录见 [advisor-draft](advisor-draft/README.md)。

## 平台跟踪（执行状态以 issue 为准）

本页是**契约与验收清单**（稳定事实）；日常执行状态、讨论与进度看 tyai 的 issue：

- 方向：[#14 [Direction] Jev 轻量化自动决策](https://github.com/Epslion404/TYAI-for-wavebench/issues/14)
- 里程碑与任务：`J1 插件类别`（#15 = J1a 类别与注册表 + J1c `advisor ask`；#19 = J1b 生命周期与包校验）／`J2 文档门禁`（#16）／`J3 候选功能与 kill criteria`（#17）／`J4 作品包接入`（#18）
- 过滤视图：`is:open label:track:advisor`
- 规则：合同与验收留在本页，执行状态与验收证据只在关联 issue 维护；下文的历史实施记录按其日期与提交解释，不作为当前进度。

## 目标与边界

- 目标：用 Jev 这类 System One 小模型，把「下一步做什么／哪里有问题／选哪个实验入口」变成**轻量、可解释、默认离线**的建议，并保证建议永远只是建议。
- 红线（来自 RFC）：advisor **不参与任何写路径、安全门、质量门与 capability 判定**；结论只落 `wavebench.decision.v1` 产物。
- 「轻量化」的具体含义：固定模型版本（`jev-1.13.0`）不用漂移别名；发送前可预览、可拒绝；只外发白名单字段；内置确定性基线可离线跑；**无 key／无网络时降级为基线**而不是失败。

## 历史合同实施记录（2026-09～10）

- [x] Jev 一手事实核验（官方 blog／docs／models／jaggedness／privacy + PyPI + LangChain）
- [x] 六项裁决：引入类别抽象；首版禁止一个包混装两类；依赖只从 `Requires-Dist` 读且 install 保持离线；同意默认按次 + 可登记 run 级并绑定 endpoint 与字段集；`advisor.external_state` 不纳入 access policy；decision artifact 不并入 `run report`
- [x] seam v2：`build_state → questions → answers → thresholds → artifact`（18 个离线用例；含迟滞/DFT 等信号侧修正）
- [x] Core 落地：`services/advisor_consent.py`（三级门 + 预览 + 同意失效判定）、`services/decision_artifacts.py`（`wavebench.decision.v1` 独占创建）、`[advisor]` 配置（开启必须给两份白名单）；PR #11 的加固已并入（36 个用例）
- [x] 内置确定性基线 `rule_advisor`（草案骨架内，22 个契约用例）
- [x] RFC 提交并在 [RFC 索引](../../rfcs/index.md) 登记为 `Draft`
- [x] J1a 类别与注册表（`07e8db2`）：`AdvisorPlugin` / `EgressDeclaration` / advisor registry + `wavebench.advisor` 入口点 + doctor；内置 `rule_advisor` 元数据；`plugin list|doctor --category {instrument|advisor}`（**仪器插件路径零改动**）
- [x] J1c `advisor ask` 端到端：`services/advisor_seam.py`（state → questions → answers → thresholds）、`services/advisor_rule.py`（内置确定性 client）、CLI `advisor ask [--preview | --accept-external-state]`；复用既有同意门与 decision artifact，未重复定义同名类型
- 已知不一致：`[advisor] enabled = true` 目前强制要求**非空 `endpoint_hosts`**，即使选用完全不外发的内置基线也得登记一个用不到的 endpoint（因为配置在学校时还不知道会挂哪个 advisor）。这是 Phase 3 已落地的配置契约，放宽需单独裁决。
- 历史替代实现：[tyai PR #20](https://github.com/Epslion404/TYAI-for-wavebench/pull/20)（分支 `feat/advisor-ask`）已关闭且未合并。Core 合同事实源为 `services/advisor_seam.py`、`services/advisor_rule.py` 及其测试；该 PR 不作为当前实现依据。

## J1 插件类别落 Core

拆成三块（2026-10-02 裁决）：**J1a 类别与注册表** 与 **J1c `advisor ask` 端到端** 收在 [#15](https://github.com/Epslion404/TYAI-for-wavebench/issues/15)；**J1b 生命周期与包校验** 单独 [#19](https://github.com/Epslion404/TYAI-for-wavebench/issues/19)。切分依据：`lifecycle.py` 是本支线唯一会碰到仪器安装路径的改动（RFC 阶段 3 本要求单独评审），与纯增量的 J1a/J1c 不在一个风险量级。

### J1a 类别与注册表（#15）

- `plugins/api.py`：`AdvisorPlugin` / `EgressDeclaration` / API 版本门 / capability 前缀（按骨架映射，既有 `InstrumentPlugin` 一行不改）
- `plugins/registry.py`：advisor registry + entry point 加载 + doctor 记录（`build_plugin_registry()` 返回不变）
- 内置 `rule_advisor` 的插件元数据（`transmits_off_machine=False`）
- CLI：`plugin list|doctor --category {instrument|advisor}`；不带 `--category` 时输出逐字节不变

### J1c `advisor ask` 端到端（#15）

- seam 落 Core：`build_state → questions → answers → thresholds → artifact`；复用既有同意门与 decision artifact，不重复定义同名类型
- 内置 `rule_advisor` 的确定性实现（词典打分、零依赖、可离线）
- CLI：`advisor ask --advisor <id> --question <id> --state <fixture.json> [--preview | --accept-external-state]`
- 接线：`[advisor]` 配置 + 同意门 + 落盘 `<run>/decisions/<ts>-<advisor>.json`
- 验收：`advisor ask --preview` 不联网；未同意／未注册字段／仅预览／endpoint 变化／字段集变化 → **外部调用次数为 0**；advisor 结果不影响 `run.json.status`、质量门与 `auto_recover`

### J1b 生命周期与包校验（#19，单独评审，当前**挂起**）

挂起理由：J3 已决定停止外部集成（见上），没有第三方 advisor 包要安装、校验或做漂移检测，因此这套按类别分派的包校验暂时没有消费方。issue 与验收门保留，重开条件随 J3 一起。

- `plugins/package_inspect.py`：entry point 组集合 → 类别（裁定 2a），缺类别／混装分别报错
- `plugins/lifecycle.py`：安装／卸载／漂移／恢复／postflight／inventory 按类别分派校验
- 验收见 [#19](https://github.com/Epslion404/TYAI-for-wavebench/issues/19)：混装包被拒、advisor 包可 install/remove、既有仪器路径零回归

## J2 文档与门禁

已收尾（`733f078`，里程碑 J2 已关闭；证据见 [#16](https://github.com/Epslion404/TYAI-for-wavebench/issues/16)）：类别契约、`advisor ask` 边界、配置入口与导航都已进站点文档，并按文档宪法标为「开发线已实现、未随正式版本发布」。另补了 `concepts/capability-model.md` 的边界事实（`advisor.*` 不参与 capability/access policy）与 `reference/cli.md` 的 advisor 入口段。

- [x] `docs/concepts/plugin-model.md` 增补 advisor 类别
- [x] 新增 `docs/development/advisor-plugin-development.md`（含「会外发数据」声明怎么写）
- [x] `docs/reference/plugins/index.md`、[RFC 索引](../../rfcs/index.md)、`mkdocs.yml` 导航同步
- [x] 验收：`audit_docs.py`、`scripts/generate_docs.py --check`、`scripts/docs_impact.py` 全过（另加 `mkdocs build --strict`）

## J3 候选决策功能与 kill criteria

已收尾（合并 PR #25 → `ff40c50`，`Closes #17`；里程碑 J3 已关闭）：

- [x] 外部核验：`typesafe-sdk` **已判定为 MIT**（PyPI `info.license_expression` + classifier；`info.license` 的 null 是旧字段）；on-prem 在官方 quickstart／models 两页无任何说法（记为 not proven）；中国大陆延迟与中文精度无凭据无法实测（记为 unmeasured）
- [x] 三个候选功能各自对照确定性基线——结论是**无法证明打得过基线**，因此停止外部集成：
  - 候选 1 `advisor.route`：保留内置离线基线，不接外部（不做 SDK、不加网络路径）
  - 候选 2 `advisor.cause`：不做
  - 候选 3 `advisor.triage`：不做
- [x] 结论写回 RFC 的 `J3 external-candidate decision` 段；维护者已接受，重开条件（版本锁定提供方 + 本地标注语料 + 可复现指标 + 既有同意门）写在同一段

注意措辞：这是「没有证据支持，所以不做」，不是「候选输了」——重开不需要推翻实验结果，只需要补齐实验。

连带影响：#19（J1b 生命周期与包校验）随之**挂起**——没有第三方 advisor 包要装。

## J4 比赛作品包侧的接入

已收尾（`Closes #18`；里程碑 J4 已关闭）：

- [x] 技能包 CLI 增加「决策建议」入口：`advise` 用代码持有的封闭选项集（`discover`／`inspect`／`analyze`），零外部调用、不产生写设备命令（PR #21）
- [x] `SKILL.md`、`README.md`、`examples/` 补离线示例：新增 `examples/README.md`，三条命令在本机实跑核验（`analyze --demo`、`analyze --file examples/demo_capture.json`、`advise --request ...`）
- [x] 与设备支线共用 M5 的离线三件套、实机验收清单与 Release 交付（清单见 [M5 实机验收清单](WaveBench_M5实机验收清单.md)）：`v1.0.0` 已发布（tag 指向 `competition`，zip 作为 asset）；M4 作品包同步同批完成（快照 `IN SYNC`）

## 与设备支线的关系

- 互不阻塞：设备支线改传输／协议，本支线改插件类别与决策；两者都落在同一份 Core 代码与同一份作品包里。
- 共用资产：`[advisor]` 配置与同意门（已落地）、假传输／离线测试风格、Release 交付方式、[仓库协作与分支约定](仓库协作与分支约定.md)。
- 排序建议：J1a → J1c → J2 可离线完成且不需要外部服务；J1b 单独评审排在其后；J3 是「要不要继续」的决策闸门，越早越好。
