# Codex CLI Task Monitor

![Agent Ops](https://img.shields.io/badge/AGENT_OPS-F97316?style=flat-square)
![Input: Local JSONL](https://img.shields.io/badge/Input-Local_JSONL-0EA5E9?style=flat-square)
![Output: AstrBot](https://img.shields.io/badge/Output-AstrBot-16A34A?style=flat-square)

**English** · [中文](README.zh-CN.md)

> A lightweight observability plugin for local Codex CLI: it turns rollout lifecycle events into clear, traceable AstrBot notifications.

This AstrBot plugin reads local Codex CLI rollout JSONL events and notifies the configured AstrBot session when a task completes normally, ends unexpectedly, or its Codex process appears to have disappeared.

The plugin is read-only: it does not invoke Codex CLI, change task state, or depend on external network services.

## Capabilities

- Watches new rollout JSONL events under `~/.codex/sessions`.
- Recognizes `task_complete` events and reports the task directory, duration, and final summary.
- Recognizes non-manual `turn_aborted` events and can ignore manual interruptions marked `reason=interrupted`.
- Reports a suspected unexpected stop when an active rollout has no terminal event and its file is no longer held by a Codex process.
- Persists file offsets and notified events to prevent duplicate notifications after polling or restart.
- Provides the `codex监控状态` command for monitoring status.

## Installation

Copy this directory into the AstrBot plugin directory:

```bash
cp -r plugins/astrbot_plugin_codex_monitor /opt/AstrBot/data/plugins/
```

This plugin has no additional Python dependencies. After AstrBot restarts, its configuration is generated at:

```text
/opt/AstrBot/data/config/astrbot_plugin_codex_monitor_config.json
```

Runtime state is stored at:

```text
/opt/AstrBot/data/plugin_data/astrbot_plugin_codex_monitor/monitor_state.json
```

Do not commit either runtime file to Git.

## Configuration

Use `config.example.json` as a reference or configure the plugin directly in the AstrBot WebUI.

| Field | Description |
| --- | --- |
| `enabled` | Enables the monitoring loop. |
| `sessions_root` | Codex rollout JSONL root. Defaults to the current user's `~/.codex/sessions` and supports `~`. |
| `target_umo` | AstrBot unified message origin, for example `qq_official:FriendMessage:<user_openid>`. Leave empty to disable notifications. |
| `poll_interval` | Polling interval in seconds; the effective minimum is 1 second. |
| `process_grace_seconds` | Delay before reporting a suspected process disappearance; the effective minimum is 5 seconds. |
| `notify_task_complete` | Sends normal-completion notifications that include a final summary. |
| `notify_unexpected_stop` | Sends notifications for empty-summary completions, aborted turns, and suspected process disappearance. |
| `ignore_human_interrupt` | Ignores `turn_aborted` events whose `reason` is `interrupted`. |

`target_umo` must be a UMO that the current AstrBot platform can actually deliver to. The plugin does not guess a destination session and never writes local session IDs or QQ OpenIDs into its default configuration.

## Event Semantics

- `event_msg` / `task_complete` with a non-empty `last_agent_message`: normal completion notification.
- `event_msg` / `task_complete` with an empty `last_agent_message`: unexpected-completion notification because there is no final response to deliver.
- `event_msg` / `turn_aborted`: handled according to `ignore_human_interrupt` and `notify_unexpected_stop`.
- An active task with no terminal event whose rollout file is no longer held by a process beyond the grace period: suspected unexpected-stop notification. This is a diagnostic signal, not an official Codex error classification.

On first startup, existing JSONL files are initialized at their current end offsets, so only appended events are processed. Deleting the state file still causes existing files to be monitored from their ends.

## Security and Privacy

Rollout files can contain prompts, tool-call arguments, paths, and final replies. The plugin sends the working directory and final summary to `target_umo`. Configure a destination you are authorized to use and restrict file permissions for both the Codex session directory and AstrBot data directory.

The repository contains only source code, metadata, configuration templates, and documentation. It contains no real UMO, rollout logs, state files, or AstrBot runtime configuration.

## Local Testing

Run this command from the AstrBot source directory:

```bash
uv run pytest tests/test_codex_monitor.py
```
