# astrbot_plugin_analysislog

> 自动分析 maafw 用户在 QQ 群上传的日志，并直接给出"问题原因 + 解决方案"。

![status](https://img.shields.io/badge/platform-OneBot%20(aiocqhttp)-blue)
![scope](https://img.shields.io/badge/scope-Group%20Only-orange)

---

## ✨ 功能

1. **被动触发**：监听 QQ 群消息，命中关键词（如"报错"、"卡住"、"识别不到"…）后，回看最近 10 分钟内该用户上传过的日志文件 → 调用 LLM 给出结论。
2. **主动触发**：群管理员可使用 `/analysis @某人` 手动触发对目标用户最近日志的分析。
3. **群文件自动接收**：通过监听 `File` 消息段，自动下载并记录用户上传的日志（支持 `.log` 与 `.zip` 自动解压）。
4. **结合技能**：插件自带 `skills/maa-punish-log-analysis/SKILL.md`，作为 LLM 的 system prompt，确保分析口径与 MAA_Punish 项目一致。
5. **回复极简**：只输出 `【问题原因】… / 【解决方案】…`，不展示分析过程。

---

## 🔧 平台 & 范围

- **平台**：仅 `aiocqhttp`（OneBot）
- **场景**：仅 QQ 群聊（私聊不响应）
- **依赖**：需要在 AstrBot 中配置至少一个 LLM Provider

---

## ⚙️ 配置项

通过 AstrBot WebUI 的插件管理面板配置。重要项：

| 配置 | 说明 | 默认 |
|---|---|---|
| `enabled` | 总开关 | `true` |
| `keywords` | 求助关键词列表 | `["报错","卡住","不动了",...]` |
| `keyword_mode` | `substring` 或 `regex` | `substring` |
| `file_name_patterns` | 日志文件名正则列表 | `gui.log` / `custom*.log` / `maa.log` / `*.zip` ... |
| `lookback_minutes` | 回看窗口（分钟） | `10` |
| `cooldown_seconds` | 同用户触发冷却（秒） | `60` |
| `nudge_replies` | 未找到日志时的随机回复 | (5 句默认) |
| `analyzing_reply` | "正在分析中..." 提示语 | 内置 |
| `max_log_chars` | 单文件送入 LLM 的字符上限 | `30000` |
| `max_total_chars` | 总字符上限 | `80000` |
| `admin_only_command` | `/analysis` 仅限群管理员/群主 | `true` |
| `source_repo_path` | **MAA_Punish 源码本地绝对路径**（必填项；留空则不做源码对照） | `""` |

---

## 🧠 工作流程

```
群消息流入
   │
   ├── 含 File 段？  → 文件名匹配日志正则 → 缓存（10min 内有效）
   │
   └── 命中关键词？  → 冷却 OK → 查该用户 10min 内的最新日志
                              │
                              ├── 没找到 → 随机催促回复
                              │
                              └── 找到 → "正在分析中..."
                                         → 下载 / 解压 zip
                                         → 拼接 SKILL.md + 日志
                                         → 调用 LLM
                                         → 回复 @用户 + 结论
```

`/analysis @某人`（仅群管理员）走同一套主流程，但跳过冷却。

---

## 📦 缓存与源码

**日志缓存**：`<AstrBot data 目录>/analysislog_cache/<group_id>/<user_id>/<ts>_<name>`
- 内存记录与磁盘文件均会自动过期清理（窗口外 + 1 天）

**maafw 项目源码**：插件**不会**自己拉取或维护源码，请自行 `git clone 项目地址.git` 到任意位置，并把绝对路径填入配置项 `source_repo_path`。
- 留空则跳过源码对照，LLM 仅基于日志作答
- 想换分支/想看本地修改版？直接 `git checkout` 或编辑文件即可，插件读最新文件
- 插件**只读**该目录，不会做任何写入操作

---

## 🛡️ 隐私

- 仅在用户主动将日志发送到群里时才采集
- 仅做单次分析，不长期持久化文本
- 不会将日志转发给除当前 LLM Provider 之外的任何第三方

---

## 🧩 技能

插件目录下的 `skills/maa-punish-log-analysis/SKILL.md` 会被 AstrBot 的 Skill Manager 自动纳入。同时它也是本插件 LLM 调用时的 system prompt 来源。

---

## License

MIT
