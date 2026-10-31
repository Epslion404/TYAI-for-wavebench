# TYAI reviewer scheduler

Versioned source for the local `TYAI-for-wavebench` review scheduler. This is a
trusted local automation tool, **not a sandbox**.

## Layout

- `runner.py` — GitHub polling, SQLite queue dispatch, OpenCode host, receipt validation.
- `queue_store.py` — durable job queue and atomic state transitions.
- `github_source.py` — read-only GitHub snapshot source.
- `process_options.py` — Windows background-process options.
- `rtm_queue.py` / `rtm_mcp.py` / `rtm_control.py` — RTM2032 resource lease and MCP surface.
- `tests/` — offline unit tests using fake hosts, fake credentials and temporary files.

Deployment wrappers (Windows scheduled task, dashboard, local launcher) are host
specific and intentionally not versioned here. Runtime state and local
configuration are ignored by git.

## Configuration

Copy `reviewer.example.json` to a local `reviewer.json` and fill in host paths.
`reviewer.json` is gitignored and must not be committed. The scheduler only
accepts `Epslion404/TYAI-for-wavebench`. Relative path fields
(`repository_path`, `policy_path`, `state_dir`, `opencode`, `hardware_python`)
are resolved against the directory containing `reviewer.json` before startup,
so the child host always receives an absolute `TYAI_RTM_STATE_DIR`.

Run from this directory:

```powershell
python -m pytest -q
python -X utf8 runner.py status
```

Repository-wide test discovery includes this directory:

```bash
python -m pytest -q tools/tyai-reviewer/tests
```

## Receipt contract

A review is marked `done` only when the receipt binds the reviewed revision and
contains usable evidence:

- non-empty `summary`;
- valid `reviewed_sha`; `pr`, `branch` and `commit` events must match the event head/branch SHA;
- non-empty `tests`, or an explicit `tests_not_required_reason`;
- list-shaped `urls`, `published_commit_shas` (valid SHAs) and `published_comment_ids`
  (positive integer IDs).

Invalid or missing evidence moves the job to `failed` without retrying, because
the host may already have written to GitHub. Preparation failures before the
agent starts remain `retry`, so they do not stay `running` until a restart.

## Host permission boundary

The scheduler runs as the current OS user and intentionally inherits that user's
filesystem and credential access. `--standalone`, a `--shared` clone, a `.venv`
junction, `--auto` permissions and the RTM2032 MCP lease are convenience and
resource controls — **none of them is an OS sandbox**. They do not protect against
reading unrelated user files, credentials or network resources.

Operators must only feed events from the trusted TYAI repository and must treat
the machine as trusted-local automation. If hard isolation is required, run the
scheduler under a dedicated OS account, VM or container with an explicit
filesystem/credential/network policy; do not claim isolation from the controls
listed above.
