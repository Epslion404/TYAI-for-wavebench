# 变更说明

一句话说清这个 PR 做了什么、为什么现在做。

## 契约

- 关联 RFC／规范：`docs/project/rfcs/...`（小节号）
- 关联 issue：Closes #
- 契约变化类型：新增（append-only）／行为等价重构／破坏性变更（说明迁移方式）

## 验证证据

命令与关键结果，逐条贴出来（不要只写"测试通过"）：

- [ ] `python -m ruff check .` →
- [ ] `python -m pytest -q`（或说明只跑了哪些聚焦用例与原因）→
- [ ] `python scripts/generate_docs.py --check` →
- [ ] `python .agents/skills/wavebench-docs/scripts/audit_docs.py --quiet-warnings` →
- [ ] 包内改动：`cd 比赛区/天翼AI电子科研Skill && python -m pytest -q` →
- [ ] 真机相关：单独授权后执行，写明「未执行」与原因

## 风险与回滚

- 影响面：哪些既有路径会受影响（后端分派、SCPI／DMM、CLI、artifact）
- 回滚方式：revert 该提交即可 / 需要额外步骤
- 已知未覆盖：明确写"未做"的部分

## 提交者自检

- [ ] 未新增默认安装的第三方依赖（可选依赖走 extras）
- [ ] 既有行为零变化的部分有回归用例
- [ ] 没有把 zip、真实仪器地址、序列号或实验数据写进跟踪文件
- [ ] 执行状态同步到对应 issue（勾选／评论），文档里不重复维护进度
