# AGENTS.md

WaveBench 是 MIT 许可的 Python 3.11+ 仪器测量台（VISA/SCPI、run plan、报告、TUI、插件 API）。本仓库的 `master` 是 **Core 主线**：上游基座 + advisor 实施；**比赛作品包只在 `competition` 线**（master 上已移除，见「已知不一致」）。

## 分支定位

- `master`（当前分支）是 Core 主线：`44506c4`（上游）→ 比赛作品包（`68148a0`／`a9e49e3`，**已在本分支移除**）→ advisor 实施（`6dc5263`／`51d6136`／`514b76f` 及后续修复）。它只存在于本地与 `tyai`：公开 fork 的 `master` 已强推回上游状态 `44506c4`。面向上游的改动从 `upstream/master` 拉聚焦分支（`upstream` remote 的 push URL 设为 `no_push`），**不要把 `master` 推到 `origin`**。
- 远端：`origin` = `Epslion404/wavebench`（公开 fork；`master` 已强推回上游状态 `44506c4`，现在等于上游镜像）；`tyai` = `Epslion404/TYAI-for-wavebench`（**private 比赛仓库**，默认分支 `master`，issue 与 PR 都在那里跟踪）；`upstream` = `Scaxlibur/WaveBench`（只读，push URL 为 `no_push`）。
- `competition` 是**比赛提交线**（孤儿根提交）：`比赛区/`（作品包）唯一事实源在这里，提交／打包／Release 都从它出；它不向上游提 PR。
- 其他分支：`docs/advisor-plugin-rfc`（上游 PR #20）、`split/agent-scope-observe-advise`（上游 PR #4）。
- CI 只在 push 到 `master`/`main` 或 PR 时触发；`competition` 不跑，本地检查要自己执行（`tyai` 的 Actions 已关闭，避免空转失败）。

## 目录职责

- `src/wavebench/`：核心实现（cli、services、drivers、transport、plugins、tui）。行为事实源依次为实现、`--help`、`run schema`、`run template --list`、`wavebench.example.toml`；文档和技能正文不能覆盖它们。
- `比赛区/`：**不在 master 上**。比赛作品包（TeleAgent Skill，含 `tianyi_electronics_skill` 与内嵌 wavebench 快照）只存在于 `competition` 分支与 Release 产物；要改包先 `git checkout competition`。
- `测试区/`：上游内容（`git rm` 掉的那个临时发现脚本已移出仓库，备份在仓库外 `D:\Documents\Code\TY_AI\tools\scratch\full_discover.py`）。
- `docs/project/`：项目带文档（不进入文档站）。advisor 线见 `docs/project/rfcs/WaveBench_advisor插件RFC.md` 与 `docs/project/design/advisor-draft/`（seam 草案、阶段 1 骨架、规划文件）；自定义设备与协议支持的规范／TODO 只在 `competition` 分支上。**协作与分支规则先读 `docs/project/design/仓库协作与分支约定.md`**。
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
- 作品包在 `competition` 分支上、于其自身目录内独立验证：`git checkout competition` → `cd 比赛区/天翼AI电子科研Skill` → `python -m pytest -q`；离线演示 `python -m tianyi_electronics_skill analyze --demo`。
- GitHub 操作手动带代理：`git -c http.proxy=http://127.0.0.1:7897 <fetch|push|ls-remote>`；`gh` 需要 `$env:HTTPS_PROXY`（本机直连会超时）。
- 仓库外的本地助手（取 PR／commit、merge、cherry-pick，自动带代理并拦「孤儿历史 merge」）：`D:\Documents\Code\TY_AI\tools\wbflow.ps1`，例如 `powershell -NoProfile -File <path> pr-cherry 20 -Repo <repo>`。

## 真机与数据边界

- 默认离线：driver/service 测试用 fake transport，实机记录不能替代可重复的离线测试。
- `run check` 不碰仪器；`doctor`、`run verify` 会查询真实设备；setter、输出切换、采集、扫频必须取得明确授权。
- 不自动执行 `*RST`，不因设置电压/幅度/频率而自动打开输出；不静默放宽安全上限，不自动追加 `--allow-50ohm`。
- 被 gitignore 覆盖：`wavebench.toml`、`/data/`、`tool-of-rei/`、`.codegraph/`、`.agents/*`（两个 skill 除外）。不要把真实仪器地址或序列号写入跟踪文件。

## 已知不一致

- `docs/development/documentation.md` 要求给文档审计器传 `--term-allowlist`，但 `audit_docs.py` 已无该参数，照抄会报未知参数。
- `master` 不含 `比赛区/`：包内容只保留在 `competition` 分支与 Release 产物中；历史提交（`68148a0`／`a9e49e3` 等）里仍可按 SHA 取到。
- 比赛作品包不再把 zip 归档提交进仓库：以后的打包走 GitHub Release（Release 挂在 `tyai`）。
- 提交信息沿用上游约定：`feat|fix|docs|test|chore|refactor|perf(scope): 说明`，正文以英文为主。
