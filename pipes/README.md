# Pipes —— 可复制的工作记忆提示词

每个目录是一个 pipe：一份 `prompt.md`，描述「用哪些工具、问什么、输出什么」。

## 为什么是提示词而不是代码

工具（`mcp_server.py` 的 5 个 tool）是通用的，**真正的知识在于怎么问**。
一个人摸索出「查技术问题要先按关键词搜原文、再按时间看前后半小时」这套问法，
写成 prompt.md 之后，别人不用重新摸索。

**这也是同事复制这套东西的最小单位**：他不需要装采集程序，
把 prompt 拿去配自己的数据源就能用。

## 怎么用

在 Claude Code 里说「按 pipes/weekly-review 的方式帮我出周报」，
或把 prompt.md 内容直接粘进去。

## 依赖的工具

| 工具 | 性质 | 用途 |
|---|---|---|
| `timeline(days_ago, hour_from, hour_to)` | 精确 | 某天用了什么应用、什么窗口 |
| `search_screen(keyword, days)` | 精确 | 屏幕文本里搜字面 |
| `recall(question, days)` | 模糊 | 语义检索，问「我最近在纠结什么」 |
| `read_report(days_ago, hour)` | 已加工 | 读已生成的分析报告 |
| `capture_status()` | 诊断 | 采集在不在跑、攒了多少 |

**精确的先用，模糊的后用。** 关键词能查到的别走语义检索——又慢又可能编。
