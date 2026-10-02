# Jev 轻量化自动决策 TODO

第二条扩展支线（与[自定义设备与协议支持](WaveBench_自定义设备与协议支持TODO.md)并列）。契约见 [advisor 插件类别 RFC](../rfcs/WaveBench_advisor插件RFC.md)，设计与阶段记录见 [advisor-draft](advisor-draft/README.md)。

## 平台跟踪（执行状态以 issue 为准）

本页是**契约与验收清单**（稳定事实）；日常执行状态、讨论与进度看 tyai 的 issue：

- 方向：[#14 [Direction] Jev 轻量化自动决策](https://github.com/Epslion404/TYAI-for-wavebench/issues/14)
- 里程碑与任务：`J1 插件类别`（#15）／`J2 文档门禁`（#16）／`J3 候选功能与 kill criteria`（#17）／`J4 作品包接入`（#18）
- 过滤视图：`is:open label:track:advisor`
- 规则：**里程碑收尾时才回来更新勾选框**；平时不要在文件里勾进度，避免与 issue 漂移。

## 目标与边界

- 目标：用 Jev 这类 System One 小模型，把「下一步做什么／哪里有问题／选哪个实验入口」变成**轻量、可解释、默认离线**的建议，并保证建议永远只是建议。
- 红线（来自 RFC）：advisor **不参与任何写路径、安全门、质量门与 capability 判定**；结论只落 `wavebench.decision.v1` 产物。
- 「轻量化」的具体含义：固定模型版本（`jev-1.13.0`）不用漂移别名；发送前可预览、可拒绝；只外发白名单字段；内置确定性基线可离线跑；**无 key／无网络时降级为基线**而不是失败。

## 已完成的合同（2026-09～10）

- [x] Jev 一手事实核验（官方 blog／docs／models／jaggedness／privacy + PyPI + LangChain）
- [x] 六项裁决：引入类别抽象；首版禁止一个包混装两类；依赖只从 `Requires-Dist` 读且 install 保持离线；同意默认按次 + 可登记 run 级并绑定 endpoint 与字段集；`advisor.external_state` 不纳入 access policy；decision artifact 不并入 `run report`
- [x] seam v2：`build_state → questions → answers → thresholds → artifact`（18 个离线用例；含迟滞/DFT 等信号侧修正）
- [x] Core 落地：`services/advisor_consent.py`（三级门 + 预览 + 同意失效判定）、`services/decision_artifacts.py`（`wavebench.decision.v1` 独占创建）、`[advisor]` 配置（开启必须给两份白名单）；PR #11 的加固已并入（36 个用例）
- [x] 内置确定性基线 `rule_advisor`（草案骨架内，22 个契约用例）
- [x] RFC 提交并在 [RFC 索引](../../rfcs/index.md) 登记为 `Draft`

## J1 插件类别落 Core

- [ ] `plugins/api.py`：`AdvisorPlugin` / `EgressDeclaration` / API 版本门 / capability 前缀（按骨架映射，既有 `InstrumentPlugin` 一行不改）
- [ ] `plugins/registry.py`：advisor registry + entry point 加载 + doctor 记录
- [ ] `plugins/package_inspect.py`、`plugins/lifecycle.py`：一个包只允许一个类别（裁定 2a）、安装／卸载／漂移／恢复按类别校验
- [ ] CLI：`plugin list|doctor --category advisor`；新增 `advisor ask --advisor <id> --question <id> [--preview | --accept-external-state]`
- [ ] 验收：`advisor ask --preview` 不联网；未同意／未注册字段／仅预览 → **外部调用次数为 0**；advisor 结果不影响 `run.json.status`、质量门与 `auto_recover`

## J2 文档与门禁

- [ ] `docs/concepts/plugin-model.md` 增补 advisor 类别
- [ ] 新增 `docs/development/advisor-plugin-development.md`（含「会外发数据」声明怎么写）
- [ ] `docs/reference/plugins/index.md`、[RFC 索引](../../rfcs/index.md)、`mkdocs.yml` 导航同步
- [ ] 验收：`audit_docs.py`、`scripts/generate_docs.py --check`、`scripts/docs_impact.py` 全过

## J3 候选决策功能与 kill criteria

- [ ] 外部核验：`typesafe-sdk` license、是否有 on-prem、中国大陆实测延迟、中文精度实测值
- [ ] 三个候选功能各自对照确定性基线，**打不过就不做**：
  - 候选 1 `advisor.route`：run 失败后，从代码持有的封闭选项集里选下一步检查入口
  - 候选 2 `advisor.cause`：采集质量异常的候选归因与概率（原因表由代码持有）
  - 候选 3 `advisor.triage`：对 run／采集给出有序档位或「人工复核」
- [ ] 结论写回本节（继续／停止／改范围），并同步 RFC 状态

## J4 比赛作品包侧的接入

- [ ] 技能包 CLI 增加「决策建议」入口：只读、默认离线、无 key 时降级为内置基线
- [ ] `SKILL.md`、`README.md`、`examples/` 补离线示例（无硬件、无 key 可跑）
- [ ] 与设备支线共用 M5 的离线三件套、实机验收清单与 Release 交付

## 与设备支线的关系

- 互不阻塞：设备支线改传输／协议，本支线改插件类别与决策；两者都落在同一份 Core 代码与同一份作品包里。
- 共用资产：`[advisor]` 配置与同意门（已落地）、假传输／离线测试风格、Release 交付方式、[仓库协作与分支约定](仓库协作与分支约定.md)。
- 排序建议：J1 → J2 可离线完成且不需要外部服务；J3 是「要不要继续」的决策闸门，越早越好。
