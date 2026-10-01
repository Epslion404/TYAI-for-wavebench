# AGENTS.md

WaveBench 是 MIT 许可的 Python 3.11+ 仪器测量台（VISA/SCPI、run plan、报告、TUI、插件 API）。本分支 `competition` 是「天翼AI杯」作品提交线，也是**比赛作品包的唯一事实源**。

## 分支定位

- `competition`（当前分支）是比赛提交线：只有一条根提交，文件树基于上游 `44506c4`，再加 `比赛区/`（作品包）、`docs/project/` 的两条线记录与 advisor 改动。提交、打包、发布都从它出；不要 merge／rebase 上游历史，也不要从这个分支向母仓库 Scaxlibur/WaveBench 开 PR。
- 远端：`origin` = `Epslion404/wavebench`（公开 fork；`master` 已强推回上游状态 `44506c4`，等于上游镜像）；`tyai` = `Epslion404/TYAI-for-wavebench`（**private 比赛仓库**，默认分支 `master`，issue 与 PR 都在那里跟踪）；`upstream` = `Scaxlibur/WaveBench`（只读，push URL 为 `no_push`）。
- `master`（本地与 `tyai`）是 **Core 主线**：上游基座 + advisor 实施，**不含 `比赛区/`**（已移除，包只在 `competition`）；面向上游的改动从 `upstream/master` 拉聚焦分支，**不要把 `master` 推到 `origin`**（公开 fork 不接受比赛内容）。
- advisor 线不单独开先遣分支：实现已并入 `master`（tyai PR #9 merged；PR #11 的 advisor 部分已镜像到 `master`）。后续 Phase 4（插件类别抽象 + lifecycle + CLI）按需要新开分支或直接提交 `master`。
- 上游 PR 分支：`docs/advisor-plugin-rfc`（#20）、`split/agent-scope-observe-advise`（#4）。
- CI 只在 push 到 `master`/`main` 或 PR 时触发，推 `competition` 不会跑，本地检查要自己执行（`tyai` 的 Actions 已关闭，避免空转）。

## 目录职责

- `src/wavebench/`：核心实现（cli、services、drivers、transport、plugins、tui）。行为事实源依次为实现、`--help`、`run schema`、`run template --list`、`wavebench.example.toml`；文档和技能正文不能覆盖它们。
- `比赛区/天翼AI电子科研Skill/`：比赛作品包（TeleAgent Skill），**唯一事实源**。`src/tianyi_electronics_skill/` 只用标准库（socket SCPI、协议 manifest、信号分析、绘图），`numpy`／`pyvisa`／`python-can` 都是可选 extras。
- `比赛区/天翼AI电子科研Skill/wavebench/`：根项目的**独立快照**。改根目录不会影响它，反之亦然；要同步就整目录覆盖。
- 作品包**不再提交 zip 归档**：打包产物走 GitHub Release（挂在 `tyai`）。
- `测试区/`：上游内容；原先放在 `测试区/.temp/` 的临时发现脚本已移出仓库，备份在 `D:\Documents\Code\TY_AI\tools\scratch\full_discover.py`。
- `docs/project/`：项目带文档（不进入文档站）。两条线的记录都在这里：自定义设备与协议支持见 `docs/project/design/WaveBench_自定义设备与协议支持规范.md` 与同名 TODO；advisor 见 `docs/project/rfcs/WaveBench_advisor插件RFC.md` 与 `docs/project/design/advisor-draft/`。
- `.agents/skills/`：两个 Agent Skill 入口 —— `wavebench/SKILL.md`（真机操作、安全门、开发验证）和 `wavebench-docs/SKILL.md`（文档治理）。根 `SKILL.md` 只是指向前者的指针；排查真机行为时按需读 `wavebench/references/`，不要凭记忆推断。

## 命令（仓库根）

```bash
python -m venv .venv        # 仓库不提交 .venv；要求 >=3.11（CI 只验证 3.11/3.12）
.venv/bin/python -m pip install -e ".[dev,analysis,tui]"    # Windows: .venv\Scripts\python.exe
.venv/bin/python -m pytest -q                # 离线全量（pythonpath=src，testpaths=tests）
.venv/bin/python -m pytest -q tests/test_run_plan.py    # 聚焦单个测试文件
.venv/bin/python -m ruff check .             # line-length=100，select E4/E7/E9/F
python scripts/generate_docs.py --check      # CI 阻断：受管理 Reference 必须与源码同步
python scripts/generate_docs.py              # 重新生成上述 Reference
python .agents/skills/wavebench-docs/scripts/audit_docs.py --quiet-warnings
python scripts/docs_impact.py --base <rev> --head HEAD   # 列出本次代码改动影响的候选文档页
mkdocs build --strict                        # 文档站，需 pip install -e ".[docs]"
```

- `pdf` extra（weasyprint）只在 Linux CI 安装，Windows 用 `dev,analysis,tui` 即可。
- 不连接仪器的命令：`run schema`、`run template --list`、`run check --plan <plan>`、`run report`。
- 作品包在其自身目录内独立验证：`cd 比赛区/天翼AI电子科研Skill` → `python -m pytest -q`；离线演示 `python -m tianyi_electronics_skill analyze --demo`。
- GitHub 操作手动带代理：`git -c http.proxy=http://127.0.0.1:7897 <fetch|push|ls-remote>`；`gh` 需要 `$env:HTTPS_PROXY`（本机直连会超时）。
- 仓库外的本地助手（取 PR／commit、merge、cherry-pick，自动带代理并拦「孤儿历史 merge」）：`D:\Documents\Code\TY_AI\tools\wbflow.ps1`，例如 `powershell -NoProfile -File <path> pr-cherry 20 -Repo <repo>`。

## 真机与数据边界

- 默认离线：driver/service 测试用 fake transport，实机记录不能替代可重复的离线测试。
- `run check` 不碰仪器；`doctor`、`run verify` 会查询真实设备；setter、输出切换、采集、扫频必须取得明确授权。
- 不自动执行 `*RST`，不因设置电压/幅度/频率而自动打开输出；不静默放宽安全上限，不自动追加 `--allow-50ohm`。
- 被 gitignore 覆盖：`wavebench.toml`、`/data/`、`tool-of-rei/`、`.codegraph/`、`.agents/*`（两个 skill 除外）。不要把真实仪器地址或序列号写入跟踪文件。

## 已知不一致

- `docs/development/documentation.md` 要求给文档审计器传 `--term-allowlist`，但 `audit_docs.py` 已无该参数，照抄会报未知参数。
- 包内的 wavebench 快照是独立副本，不会随 `src/wavebench/` 自动更新；要同步就整目录覆盖并重跑包内测试。
- 提交信息沿用上游约定：`feat|fix|docs|test|chore|refactor|perf(scope): 说明`，正文以英文为主。
