# AGENTS.md

WaveBench 是 MIT 许可的 Python 3.11+ 仪器测量台（VISA/SCPI、run plan、报告、TUI、插件 API）。本仓库是它的比赛提交分支。

## 分支定位

- `competition`（当前分支）是「天翼AI杯」作品提交分支：只有一条根提交，文件树基于上游 `44506c4`，再加 `比赛区/` 与后续 docs／advisor 改动。不要把上游历史 merge/rebase 进来，也不要从这个分支向母仓库 Scaxlibur/WaveBench 开 PR。
- 远端：`origin` = `Epslion404/wavebench`（fork，上游 PR 的来源）；`tyai` = `Epslion404/TYAI-for-wavebench`（**private 比赛仓库**，默认分支 `master`，issue 与 PR 都在那里跟踪）。
- `master`（fork 与 tyai 上）已经不只是上游镜像：它现在是「上游基座 + 比赛内容」（`68148a0` 天翼Start_1.0、`a9e49e3` add range of finding），并已用上游 PR #27 提出。改动 `master` 前先确认是否要让它继续保持干净镜像。
- 其他在用分支：`feat/advisor-plugin-category`（advisor 线，基于 `master`，PR 见 tyai #9）；`docs/advisor-plugin-rfc`、`split/agent-scope-observe-advise` 分别是上游 PR #20／#4 的分支。
- CI 只在 push 到 `master`/`main` 或 PR 时触发，推 `competition` 不会跑，本地检查要自己执行。

## 目录职责

- `src/wavebench/`：核心实现（cli、services、drivers、transport、plugins、tui）。行为事实源依次为实现、`--help`、`run schema`、`run template --list`、`wavebench.example.toml`；文档和技能正文不能覆盖它们。
- `比赛区/天翼AI电子科研Skill/`：比赛作品包（TeleAgent Skill）。`src/tianyi_electronics_skill/` 只用标准库（socket SCPI + 信号分析），`numpy`/`pyvisa` 是可选 extras。
- `比赛区/天翼AI电子科研Skill/wavebench/`：根项目的**独立快照**。与根目录同名的 467 个文件内容逐字节相同，另有 `skills/` 与根 `.agents/skills/` 重复；根目录缺少 `tool-of-rei/release-notes/`。改根目录不会影响它，反之亦然，要同步就整目录覆盖。
- `比赛区/天翼AI电子科研Skill.zip`（8.66 MB）：作品包的打包副本。改目录后必须重新打包，否则内容漂移。
- `测试区/.temp/full_discover.py`：临时脚本（pyvisa 枚举 + 硬编码网段 `10.17.216.0/24` 的 SCPI 端口扫描），不是测试套件。
- `docs/project/`：项目带文档（不进入文档站）。当前两条线的记录：自定义设备与协议支持见 `docs/project/design/WaveBench_自定义设备与协议支持规范.md` 与同名 TODO；advisor 线见 `docs/project/rfcs/WaveBench_advisor插件RFC.md` 与 `docs/project/design/advisor-draft/`（seam 草案、阶段 1 骨架、规划文件）。
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
- 作品包在其自身目录内独立验证：`python -m pytest -q`；离线演示 `python -m tianyi_electronics_skill analyze --demo`。
- GitHub 操作手动带代理：`git -c http.proxy=http://127.0.0.1:7897 <fetch|push|ls-remote>`；`gh` 需要 `$env:HTTPS_PROXY`（本机直连会超时）。
- 仓库外的本地助手（取 PR／commit、merge、cherry-pick，自动带代理并拦「孤儿历史 merge」）：`D:\Documents\Code\TY_AI\tools\wbflow.ps1`，例如 `powershell -NoProfile -File <path> pr-cherry 20 -Repo <repo>`。

## 真机与数据边界

- 默认离线：driver/service 测试用 fake transport，实机记录不能替代可重复的离线测试。
- `run check` 不碰仪器；`doctor`、`run verify` 会查询真实设备；setter、输出切换、采集、扫频必须取得明确授权。
- 不自动执行 `*RST`，不因设置电压/幅度/频率而自动打开输出；不静默放宽安全上限，不自动追加 `--allow-50ohm`。
- 被 gitignore 覆盖：`wavebench.toml`、`/data/`、`tool-of-rei/`、`.codegraph/`、`.agents/*`（两个 skill 除外）。不要把真实仪器地址或序列号写入跟踪文件。

## 已知不一致

- `docs/development/documentation.md` 要求给文档审计器传 `--term-allowlist`，但 `audit_docs.py` 已无该参数，照抄会报未知参数。
- `比赛区/天翼AI电子科研Skill/README.md` 的安装示例用的是作者本机绝对路径，按当前目录替换。
- 提交信息沿用上游约定：`feat|fix|docs|test|chore|refactor|perf(scope): 说明`，正文以英文为主。
- `master` 已带比赛内容（与旧约定「master = 干净上游镜像」不同），上游 PR #27 由它提出且 diff 超过 300 个文件。
- `competition` 里的 `比赛区/` 落后于 `master` 的版本：`a9e49e3` 增加了 CAN／协议示例（`examples/protocols/`）、采集产物、更新了 README／SKILL.md 与 zip，尚未同步过来。
