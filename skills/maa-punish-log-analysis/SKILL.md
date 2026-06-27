---
name: maa-punish-log-analysis
description: 分析用户给出的本地日志目录或日志文件路径，结合 MAA_Punish（法奥斯之矛）仓库定位任务卡死、识别失败、Pipeline 与自定义逻辑问题。主日志为 gui.log（cfa 图形界面）、custom.log（Python 自定义）、debug/maa.log（框架运行时）。不下载或解压 zip；在用户给出日志路径、贴日志片段、反馈 bug、排查识别或流水线问题时使用。
---

# MAA_Punish 法奥斯之矛 — 本地日志分析

## 适用范围

- 仓库：`MAA_Punish`（战双帕弥什小助手，Python 自定义 + MaaFramework），又名法奥斯之矛。
- **输入**：用户提供的**目录路径**（推荐）或具体 `.log` 文件路径。**不**处理 GitHub issue 附件 zip。
- 若用户只给目录，在该目录下按下方 **Log Map** 查找标准文件名；若路径不存在或缺少关键文件，先列出目录再说明缺什么证据。

## 标准日志文件（按优先级阅读）

| 文件 | 含义 |
|------|------|
| `gui.log` | **MFW-cfa** 图形界面侧日志：配置加载、任务发起、界面与编排相关线索。 |
| `custom.log` | **Python 自定义**（`assets/agent`）侧日志：自定义识别/动作的打印与异常。 |
| `maa.log` | **MaaFramework** 核心运行时（`debug/maa.log`）：Pipeline 节点、识别、动作、控制器、task_id 等。 |

说明：`agent/logger_component.py` 默认可能写入 `debug/custom_YYYYMMDD.log`；若用户统一导出为 `custom.log`，以用户约定为准。

## 工作流

1. **解析路径** — 列出目录下 `*.log`、`on_error/`、`config/` 等；判断文件类别。
2. **建立时间线** — 从用户描述取版本、平台、控制器类型、任务名、现象。在 `gui.log` 查任务提交，`maa.log` 用 `task_id` 串联运行，`custom.log` 查同时段 Python 输出。
3. **关联代码与资源** — `assets/interface.json`、`assets/tasks/*.json`、`assets/resource/**/pipeline/**/*.jsonc`、`agent/**/*.py`。
4. **过滤证据** — 高价值关键词：`Tasker.Task.Starting` / `Succeeded` / `Failed`，`Node.Recognition.Failed`，`Node.Action.Failed`，`timeout`，`Warn` / `Error` / `Fatal`。
5. **可选材料** — `on_error/` 截图、配置快照。

## 根因与输出

先区分：框架层（`maa.log`）、界面层（`gui.log`）、自定义扩展（`custom.log`）哪一层最先出现异常。结论需有日志摘录或节点名级别依据。

## 建议的回答结构

```markdown
## 现象与范围
## 日志证据（gui / custom / maa）
## 时间线与 task 关联
## 根因判断
## 建议（配置 / 资源 / 代码 / 升级）
## 置信度与缺失证据
```

## 仓库位置

`C:\Users\DT\data\workspaces\default_FriendMessage_FEF8CD487785570FF3DE6A2C65150FB1\MAA_Punish`
