---
name: maa-punish-log-analysis
description: 分析 MAA_Punish（法奥斯之矛）用户上传的日志，定位任务卡死、识别失败、Pipeline 与自定义逻辑问题。主日志为 gui.log（MFW-cfa 图形界面）、custom.log（Python 自定义）、debug/maa.log（框架运行时）。上游 astrbot_plugin_analysislog 插件会负责下载与解压 zip，并把仓库源码路径作为变量注入到本提示中；本技能在 QQ 群求助场景被插件自动调用。
---

# MAA_Punish 法奥斯之矛 — 日志分析

## 适用场景

- 仓库：`MAA_Punish`（战双帕弥什小助手，Python 自定义 + MaaFramework），又名"法奥斯之矛"。
- 输入由上游 **astrbot_plugin_analysislog** 插件提供：插件会自动下载 QQ 群用户上传的日志文件，若是 `.zip` 则解压后再喂入；你不需要去找 zip。
- 上游同时会在 prompt 中告诉你 MAA_Punish 仓库源码所在的绝对路径（由插件维护的本地副本），你可以基于该路径检索资源、Pipeline、Python 自定义代码作为对照证据。

## 标准日志文件（按优先级阅读）

| 文件 | 来源 | 含义 |
|------|------|------|
| `gui.log` | MFW-cfa 图形界面 | 配置加载、任务发起、界面/编排线索 |
| `custom.log` | Python 自定义（`assets/agent`） | 自定义识别/动作的打印与异常 |
| `maa.log`（或 `debug/maa.log`） | MaaFramework 核心运行时 | Pipeline 节点、识别、动作、控制器、`task_id` |

说明：`agent/logger_component.py` 可能写入 `debug/custom_YYYYMMDD.log`；若用户已统一导出为 `custom.log`，以用户约定为准。

## 高价值关键词

`Tasker.Task.Starting` / `Tasker.Task.Succeeded` / `Tasker.Task.Failed` / `Node.Recognition.Failed` / `Node.Action.Failed` / `timeout` / `Warn` / `Error` / `Fatal`。

## 工作流（最简版）

1. **判层**：先确定异常**最先**出现在哪一层 —— 框架层（`maa.log`）/ 界面层（`gui.log`）/ 自定义扩展（`custom.log`）。
2. **关联 task_id**：通过 `task_id` 把 `gui.log` 的任务发起与 `maa.log` 的节点执行串起来。
3. **对照源码**：用上游注入的仓库路径，按需查阅：
   - `assets/interface.json`、`assets/tasks/*.json`
   - `assets/resource/**/pipeline/**/*.jsonc`
   - `agent/**/*.py`
4. **下结论**：结论必须有日志摘录或节点名作为依据。

## 输出要求（被 astrbot_plugin_analysislog 调用时必须遵守）

**最终回复格式严格如下，不展示分析过程、不复述日志、不加免责声明：**

```
【问题原因】<一句话讲清楚根因>
【解决方案】
1. <第一步，可执行>
2. <第二步，可执行>
3. <更多步骤，按需>
```

若**信息不足**以判断，则输出：

```
【问题原因】证据不足
【解决方案】请补充：<具体缺什么日志/截图/版本>
```

## 仓库源码路径

上游插件会在 system prompt 中以变量形式提供，例如：
```
MAA_Punish 源码位置：<由插件运行时填充的绝对路径>
```
若 prompt 中未提供该路径，则跳过源码对照步骤，仅基于日志本身作答。
