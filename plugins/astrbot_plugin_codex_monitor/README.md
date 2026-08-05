# Codex CLI 任务监控插件

![Agent Ops](https://img.shields.io/badge/AGENT_OPS-F97316?style=flat-square)
![Input: Local JSONL](https://img.shields.io/badge/Input-Local_JSONL-0EA5E9?style=flat-square)
![Output: AstrBot](https://img.shields.io/badge/Output-AstrBot-16A34A?style=flat-square)

> 一个面向本机 Codex CLI 的轻量可观测性插件：把 rollout 生命周期转换成清晰、可追踪的 AstrBot 通知。

这个 AstrBot 插件读取本机 Codex CLI 的 rollout JSONL 事件，在任务正常结束、异常结束或 Codex 进程疑似消失时，向配置的 AstrBot 会话发送通知。

插件只读本机事件文件，不调用 Codex CLI、不修改任务状态，也不依赖外部网络服务。

## 功能

- 监控 `~/.codex/sessions` 下新增的 rollout JSONL 事件
- 识别 `task_complete`，报告任务目录、耗时和最终摘要
- 识别非人为 `turn_aborted`，并可忽略 `reason=interrupted` 的人工中断
- 在没有终结事件且关联 rollout 文件不再被 Codex 进程持有时报告疑似异常中断
- 记录文件偏移和已通知事件，避免重启或轮询造成重复通知
- 提供 `codex监控状态` 命令查看监控状态

## 安装

把本目录复制到 AstrBot 插件目录：

```bash
cp -r plugins/astrbot_plugin_codex_monitor /opt/AstrBot/data/plugins/
```

本插件没有额外的 Python 依赖。重启 AstrBot 后，配置会生成在：

```text
/opt/AstrBot/data/config/astrbot_plugin_codex_monitor_config.json
```

运行状态会保存到：

```text
/opt/AstrBot/data/plugin_data/astrbot_plugin_codex_monitor/monitor_state.json
```

不要把这两个运行文件提交到 Git。

## 配置

可以参考 `config.example.json`，或者直接在 AstrBot WebUI 中填写插件配置。

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用监控循环 |
| `sessions_root` | Codex rollout JSONL 根目录，默认是当前用户的 `~/.codex/sessions`，支持 `~` |
| `target_umo` | AstrBot 统一消息来源，例如 `qq_official:FriendMessage:<user_openid>`；留空时不发送通知 |
| `poll_interval` | 轮询间隔，实际最低为 1 秒 |
| `process_grace_seconds` | 报告疑似进程消失前的等待时间，实际最低为 5 秒 |
| `notify_task_complete` | 是否通知带有最终摘要的正常完成事件 |
| `notify_unexpected_stop` | 是否通知空摘要完成、异常中止和疑似进程消失 |
| `ignore_human_interrupt` | 是否忽略 `turn_aborted` 且 `reason` 为 `interrupted` 的人工中断 |

`target_umo` 必须使用当前 AstrBot 平台实际可发送的 UMO。插件不会替用户猜测目标会话，也不会把本机的会话 ID 或 QQ OpenID 写入默认配置。

## 事件判定

- `event_msg` / `task_complete` 且 `last_agent_message` 非空：正常完成通知。
- `event_msg` / `task_complete` 且 `last_agent_message` 为空：异常完成通知，因为没有可交付的最终回复。
- `event_msg` / `turn_aborted`：按 `ignore_human_interrupt` 和 `notify_unexpected_stop` 配置处理。
- 活跃任务没有终结事件，且 rollout 文件不再被进程持有超过宽限期：疑似异常中断通知。这个判定是诊断信号，不等同于 Codex 官方错误分类。

插件首次启动时会把已有 JSONL 文件的偏移初始化到文件末尾，因此只处理启动后追加的事件。状态文件被删除后，已有文件仍会从末尾开始监控。

## 安全与隐私

rollout 文件可能包含提示词、工具调用参数、路径和最终回复。插件会把工作目录和最终摘要发送到 `target_umo`，请只把目标设置为你有权接收的会话，并限制 Codex 会话目录和 AstrBot 数据目录的文件权限。

仓库只包含源码、元数据、配置模板和文档，不包含真实 UMO、rollout 日志、状态文件或 AstrBot 运行配置。

## 本地测试

在 AstrBot 源码目录执行：

```bash
uv run pytest tests/test_codex_monitor.py
```
