# 清理记录

## 2026-07-06

已完成的低风险整理：

- 根目录重复旧 `shared/` 已移到 `../_archive/legacy_root_shared_20260706/`。
- 根目录旧交接文档已移到 `../_archive/handoff_20260706/交接文档.md`。
- 工作台旧运行数据已移到 `_local_archive/old_runtime_20260706/`。
- Python `__pycache__` 缓存已删除。

当前保留的运行入口：

- 后端：`app.py`
- 前端：`static/index.html`
- 手册匹配：`shared/manual_search.py`
- 当前运行数据库：`runtime_data/workbench.db`

未清理的范围：

- `../故障手册知识库/` 是独立仓库，当前有大量未提交生成内容，需要单独备份和验证后再整理。
- `.venv/` 保留，方便本地继续运行和测试。
