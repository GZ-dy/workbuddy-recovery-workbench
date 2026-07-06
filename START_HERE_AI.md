# AI 接手入口

本目录是当前正在运行的“AI故障恢复验证工作台”。后续 AI 优先从这里开始，不要先去改兄弟目录或旧目录。

## 当前权威入口

- 后端入口：`app.py`
- 规则解析：`rules.py`
- 前端页面：`static/index.html`
- 工作台测试：`tests/`
- 启动脚本：`start_workbench_8123.bat`
- 当前运行数据库：`runtime_data/workbench.db`，不要提交，不要手工改

## 手册边界

- 真实手册索引只读自：`../故障手册知识库/workbuddy/data/manual_index.json`
- 工作台通过 `manual_links_for_alarm(...)` 和 `/manuals/{id}` 使用手册
- 不要复制手册库生成逻辑到 `app.py`
- 不要改旧 `高频故障` 路径作为新功能入口

## 易错点

- 根目录 `F:\workbuddy\多域AI故障\本地` 不是一个有效 Git 仓库。
- `故障手册知识库` 是独立仓库，里面有大量生成文件和手册数据，清理前必须单独验证。
- 不要把 `.venv/`、`runtime_data/`、`.data/`、`output/`、`*.db`、`mail_config.local.bat` 提交。

## 最小验证

```powershell
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

运行：

```powershell
.\start_workbench_8123.bat
```
