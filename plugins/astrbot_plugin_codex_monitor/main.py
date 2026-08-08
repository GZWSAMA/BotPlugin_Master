import asyncio
import json
import os
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools


PLUGIN_NAME = "astrbot_plugin_codex_monitor"
DEFAULT_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"


@dataclass
class ActiveTurn:
    turn_id: str
    rollout_path: str
    cwd: str
    started_at: str
    first_prompt: str
    process_seen: bool
    missing_since: float | None = None


class CodexMonitorPlugin(Star):
    """Monitor Codex rollout events and notify a configured AstrBot target."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.enabled = bool(self.config.get("enabled", True))
        self.sessions_root = Path(
            str(self.config.get("sessions_root") or DEFAULT_SESSIONS_ROOT)
        ).expanduser()
        self.target_umo = str(self.config.get("target_umo") or "").strip()
        self.poll_interval = max(1.0, float(self.config.get("poll_interval", 3.0)))
        self.process_grace_seconds = max(
            5.0,
            float(self.config.get("process_grace_seconds", 20.0)),
        )
        self.notify_task_complete = bool(
            self.config.get("notify_task_complete", True)
        )
        self.notify_unexpected_stop = bool(
            self.config.get("notify_unexpected_stop", True)
        )
        self.ignore_human_interrupt = bool(
            self.config.get("ignore_human_interrupt", True)
        )
        self.auto_continue_on_capacity = bool(
            self.config.get("auto_continue_on_capacity", True)
        )
        self.auto_continue_delay_seconds = max(
            0.0, float(self.config.get("auto_continue_delay_seconds", 3.0))
        )
        self.auto_continue_ack_timeout_seconds = max(
            1.0,
            float(self.config.get("auto_continue_ack_timeout_seconds", 12.0)),
        )
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.state_path = self.data_dir / "monitor_state.json"
        self.offsets: dict[str, int] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.active_turns: dict[str, ActiveTurn] = {}
        self.notified_event_ids: set[str] = set()
        self.capacity_retries: dict[str, int] = {}
        self._retry_tasks: set[asyncio.Task[None]] = set()
        self._pending_retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._auto_input_paths: set[str] = set()
        self._retry_started_events: dict[str, asyncio.Event] = {}
        self._load_state()
        self._monitor_task = asyncio.create_task(self._monitor_loop())

    def _load_state(self) -> None:
        if not self.state_path.exists():
            return
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("%s failed to load state", PLUGIN_NAME)
            return
        offsets = payload.get("offsets", {})
        notified = payload.get("notified_event_ids", [])
        if isinstance(offsets, dict):
            self.offsets = {
                str(path): max(0, int(offset)) for path, offset in offsets.items()
            }
        if isinstance(notified, list):
            self.notified_event_ids = {str(event_id) for event_id in notified}

    def _save_state(self) -> None:
        payload = {
            "offsets": self.offsets,
            "notified_event_ids": sorted(self.notified_event_ids)[-5000:],
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    async def _monitor_loop(self) -> None:
        if not self.enabled:
            return
        self.sessions_root.mkdir(parents=True, exist_ok=True)
        self._initialize_offsets_for_existing_files()
        logger.info(
            "%s monitoring %s and notifying %s",
            PLUGIN_NAME,
            self.sessions_root,
            self.target_umo,
        )
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s monitor iteration failed", PLUGIN_NAME)
            await asyncio.sleep(self.poll_interval)

    def _initialize_offsets_for_existing_files(self) -> None:
        changed = False
        for path in self.sessions_root.rglob("*.jsonl"):
            key = str(path)
            if key not in self.offsets:
                self.offsets[key] = path.stat().st_size
                changed = True
        if changed:
            self._save_state()

    async def _poll_once(self) -> None:
        for path in self.sessions_root.rglob("*.jsonl"):
            await self._read_new_events(path)
        await self._check_unexpected_process_stops()
        self._save_state()

    async def _read_new_events(self, path: Path) -> None:
        key = str(path)
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return
        offset = self.offsets.get(key, 0)
        if size < offset:
            offset = 0
        if size == offset:
            return
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            for line in handle:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                await self._handle_event(path, event)
            self.offsets[key] = handle.tell()

    async def _handle_event(self, path: Path, event: dict[str, Any]) -> None:
        event_type = event.get("type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            return
        payload_type = payload.get("type")
        timestamp = str(event.get("timestamp") or "")

        if event_type == "session_meta":
            self.metadata[str(path)] = {
                "cwd": str(payload.get("cwd") or ""),
                "session_id": str(payload.get("session_id") or payload.get("id") or ""),
            }
            return

        passthrough = payload.get("internal_chat_message_metadata_passthrough")
        if not isinstance(passthrough, dict):
            passthrough = {}
        turn_id = str(payload.get("turn_id") or passthrough.get("turn_id") or "")
        if event_type == "response_item" and payload_type == "message":
            role = payload.get("role")
            if role == "user" and turn_id:
                path_key = str(path)
                if path_key in self._auto_input_paths:
                    self._auto_input_paths.discard(path_key)
                    started = self._retry_started_events.get(path_key)
                    if started is not None:
                        started.set()
                else:
                    pending = self._pending_retry_tasks.pop(path_key, None)
                    if pending is not None and not pending.done():
                        pending.cancel()
                prompt = self._extract_message_text(payload)
                meta = self.metadata.get(str(path), {})
                self.active_turns[turn_id] = ActiveTurn(
                    turn_id=turn_id,
                    rollout_path=str(path),
                    cwd=meta.get("cwd", ""),
                    started_at=timestamp,
                    first_prompt=prompt[:300],
                    process_seen=self._rollout_process_running(path),
                )
            return

        if event_type == "event_msg" and payload_type == "task_started":
            path_key = str(path)
            if path_key in self._auto_input_paths:
                started = self._retry_started_events.get(path_key)
                if started is not None:
                    started.set()
            return

        if event_type != "event_msg" or not turn_id:
            return
        event_id = f"{payload_type}:{turn_id}"
        if event_id in self.notified_event_ids:
            self.active_turns.pop(turn_id, None)
            return

        if payload_type == "task_complete":
            self.active_turns.pop(turn_id, None)
            if self._is_capacity_error(payload):
                self.notified_event_ids.add(event_id)
                await self._handle_capacity_error(path, payload, timestamp)
                return
            last_message = str(payload.get("last_agent_message") or "").strip()
            if not last_message:
                if self.notify_unexpected_stop:
                    self.notified_event_ids.add(event_id)
                    await self._notify(
                        self._format_empty_task_complete(path, payload, timestamp)
                    )
                return
            self.capacity_retries.pop(str(path), None)
            if self.notify_task_complete:
                self.notified_event_ids.add(event_id)
                await self._notify(
                    self._format_task_complete(path, payload, timestamp)
                )
            return

        if payload_type == "turn_aborted":
            self.active_turns.pop(turn_id, None)
            reason = str(payload.get("reason") or "unknown")
            if self.ignore_human_interrupt and reason == "interrupted":
                return
            if self.notify_unexpected_stop:
                self.notified_event_ids.add(event_id)
                await self._notify(
                    self._format_abnormal_event(path, payload, timestamp)
                )

    @staticmethod
    def _is_capacity_error(payload: dict[str, Any]) -> bool:
        error = payload.get("error")
        if not isinstance(error, dict):
            return False
        info = str(error.get("codex_error_info") or "").lower()
        message = str(error.get("message") or "").lower()
        return info == "server_overloaded" or (
            "selected model is at capacity" in message
            and "different model" in message
        )

    async def _handle_capacity_error(
        self, path: Path, payload: dict[str, Any], timestamp: str
    ) -> None:
        key = str(path)
        attempt = self.capacity_retries.get(key, 0)
        if not self.auto_continue_on_capacity:
            if self.notify_unexpected_stop:
                await self._notify(self._format_capacity_error(path, payload, timestamp, None))
            return
        pending = self._pending_retry_tasks.get(key)
        if pending is not None and not pending.done():
            return
        attempt += 1
        self.capacity_retries[key] = attempt
        # Keep the backoff bounded without allowing the exponent itself to
        # overflow now that retries intentionally have no count limit.
        exponent = min(max(attempt - 1, 0), 10)
        delay = min(self.auto_continue_delay_seconds * (2**exponent), 300.0)
        task = asyncio.create_task(self._retry_capacity_turn(path, payload, attempt, delay))
        self._retry_tasks.add(task)
        self._pending_retry_tasks[key] = task
        task.add_done_callback(lambda done: self._on_retry_done(key, done))
        if self.notify_unexpected_stop:
            await self._notify(
                self._format_capacity_error(path, payload, timestamp, attempt)
                + f"\n将在 {delay:.0f} 秒后自动发送“继续”，等待 Codex 新回合确认。"
            )

    def _on_retry_done(self, key: str, task: asyncio.Task[None]) -> None:
        self._retry_tasks.discard(task)
        if self._pending_retry_tasks.get(key) is task:
            self._pending_retry_tasks.pop(key, None)

    async def _retry_capacity_turn(
        self,
        path: Path,
        payload: dict[str, Any],
        attempt: int,
        delay: float,
    ) -> None:
        key = str(path)
        started = self._retry_started_events.get(key)
        if started is None:
            started = asyncio.Event()
            self._retry_started_events[key] = started
        try:
            await asyncio.sleep(delay)
            target = self._find_codex_tmux_target(path)
            if target is None:
                if self.notify_unexpected_stop:
                    await self._notify(
                        self._format_capacity_error(path, payload, "", attempt)
                        + "\n未找到仍在运行的 Codex tmux pane，无法自动继续。"
                    )
                return
            self._auto_input_paths.add(str(path))
            result = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", target, "-l", "继续",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await result.communicate()
            if result.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", "replace").strip())
            result = await asyncio.create_subprocess_exec(
                "tmux", "send-keys", "-t", target, "Enter",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await result.communicate()
            if result.returncode != 0:
                raise RuntimeError(stderr.decode("utf-8", "replace").strip())
            try:
                await asyncio.wait_for(
                    started.wait(), timeout=self.auto_continue_ack_timeout_seconds
                )
            except asyncio.TimeoutError:
                self._auto_input_paths.discard(key)
                if self.notify_unexpected_stop:
                    await self._notify(
                        self._format_capacity_error(path, payload, "", attempt)
                        + "\n已发送“继续”，但在等待时间内未确认 Codex 新回合开始。"
                    )
                return
        except (OSError, RuntimeError) as error:
            self._auto_input_paths.discard(str(path))
            if self.notify_unexpected_stop:
                await self._notify(
                    self._format_capacity_error(path, payload, "", attempt)
                    + f"\n自动继续发送失败: {error}"
                )
            return
        finally:
            if self._retry_started_events.get(key) is started:
                self._retry_started_events.pop(key, None)
            if not started.is_set():
                self._auto_input_paths.discard(key)
        if self.notify_unexpected_stop:
            await self._notify(
                self._format_capacity_error(path, payload, "", attempt)
                + f"\n✅ Codex 已接受自动“继续”，新回合已在 pane {target} 开始。"
            )

    @staticmethod
    def _find_codex_tmux_target(rollout_path: Path | str) -> str | None:
        expected = os.path.realpath(str(rollout_path))
        tty_names: set[str] = set()
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                descriptors = list((entry / "fd").iterdir())
            except OSError:
                continue
            holds_rollout = False
            for descriptor in descriptors:
                try:
                    if os.path.realpath(os.readlink(descriptor)) == expected:
                        holds_rollout = True
                        break
                except OSError:
                    continue
            if not holds_rollout:
                continue
            try:
                command = Path(entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
                tty = os.path.realpath(os.readlink(entry / "fd/0"))
            except (OSError, UnicodeDecodeError):
                continue
            if "codex" in command.lower() and tty.startswith("/dev/pts/"):
                tty_names.add(tty)
        if not tty_names:
            return None
        try:
            output = subprocess.check_output(
                ["tmux", "list-panes", "-a", "-F", "#{pane_tty}\t#{session_name}:#{window_index}.#{pane_index}"],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            return None
        for line in output.splitlines():
            tty, separator, target = line.partition("\t")
            if separator and tty in tty_names:
                return target
        return None

    @staticmethod
    def _extract_message_text(payload: dict[str, Any]) -> str:
        content = payload.get("content", [])
        if not isinstance(content, list):
            return ""
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") in {
                "input_text",
                "output_text",
                "text",
            }:
                parts.append(str(item.get("text") or ""))
        return " ".join(parts).strip()

    @staticmethod
    def _rollout_process_running(rollout_path: Path | str) -> bool:
        expected = os.path.realpath(str(rollout_path))
        proc = Path("/proc")
        for entry in proc.iterdir():
            if not entry.name.isdigit():
                continue
            fd_dir = entry / "fd"
            try:
                descriptors = list(fd_dir.iterdir())
            except OSError:
                continue
            for descriptor in descriptors:
                try:
                    target = os.path.realpath(os.readlink(descriptor))
                except OSError:
                    continue
                if target == expected:
                    return True
        return False

    async def _check_unexpected_process_stops(self) -> None:
        if not self.notify_unexpected_stop or not self.active_turns:
            return
        now = time.monotonic()
        for turn_id, turn in list(self.active_turns.items()):
            if self._rollout_process_running(turn.rollout_path):
                turn.process_seen = True
                turn.missing_since = None
                continue
            if not turn.process_seen:
                continue
            if turn.missing_since is None:
                turn.missing_since = now
                continue
            if now - turn.missing_since < self.process_grace_seconds:
                continue
            event_id = f"unexpected_process_stop:{turn_id}"
            if event_id not in self.notified_event_ids:
                self.notified_event_ids.add(event_id)
                await self._notify(
                    "⚠️ Codex CLI 任务疑似非人为异常中断\n"
                    f"Turn: {turn.turn_id}\n"
                    f"工作目录: {turn.cwd or '未知'}\n"
                    f"开始时间: {turn.started_at or '未知'}\n"
                    f"任务摘要: {turn.first_prompt or '未能读取'}\n"
                    "检测依据: 活跃任务尚无 task_complete/turn_aborted 事件，"
                    f"且 Codex 进程已消失超过 {self.process_grace_seconds:.0f} 秒。"
                )
            self.active_turns.pop(turn_id, None)

    def _format_empty_task_complete(
        self,
        path: Path,
        payload: dict[str, Any],
        timestamp: str,
    ) -> str:
        duration_ms = payload.get("duration_ms")
        duration = (
            f"{float(duration_ms) / 1000:.1f} 秒"
            if isinstance(duration_ms, int | float)
            else "未知"
        )
        meta = self.metadata.get(str(path), {})
        return (
            "⚠️ Codex CLI 任务异常结束\n"
            f"Turn: {payload.get('turn_id', '未知')}\n"
            f"工作目录: {meta.get('cwd') or '未知'}\n"
            f"结束时间: {timestamp or '未知'}\n"
            f"运行时长: {duration}\n"
            "异常依据: 收到了 task_complete 事件，但 last_agent_message "
            "为空，未生成可交付的最终回复。"
        )

    def _format_task_complete(
        self,
        path: Path,
        payload: dict[str, Any],
        timestamp: str,
    ) -> str:
        duration_ms = payload.get("duration_ms")
        duration = (
            f"{float(duration_ms) / 1000:.1f} 秒"
            if isinstance(duration_ms, int | float)
            else "未知"
        )
        last_message = str(payload.get("last_agent_message") or "").strip()
        if len(last_message) > 500:
            last_message = last_message[:500] + "…"
        meta = self.metadata.get(str(path), {})
        return (
            "✅ Codex CLI 任务已结束\n"
            f"Turn: {payload.get('turn_id', '未知')}\n"
            f"工作目录: {meta.get('cwd') or '未知'}\n"
            f"结束时间: {timestamp or '未知'}\n"
            f"运行时长: {duration}\n"
            f"最终摘要: {last_message}"
        )

    def _format_abnormal_event(
        self,
        path: Path,
        payload: dict[str, Any],
        timestamp: str,
    ) -> str:
        meta = self.metadata.get(str(path), {})
        return (
            "⚠️ Codex CLI 任务异常结束\n"
            f"Turn: {payload.get('turn_id', '未知')}\n"
            f"工作目录: {meta.get('cwd') or '未知'}\n"
            f"时间: {timestamp or '未知'}\n"
            f"原因: {payload.get('reason') or 'unknown'}"
        )

    def _format_capacity_error(
        self,
        path: Path,
        payload: dict[str, Any],
        timestamp: str,
        attempt: int | None,
    ) -> str:
        error = payload.get("error")
        message = error.get("message") if isinstance(error, dict) else None
        suffix = (
            f"\n自动继续次数: {attempt}（无上限）"
            if attempt is not None
            else "\n自动继续未启用"
        )
        return (
            "⚠️ Codex CLI 模型容量不足\n"
            f"Turn: {payload.get('turn_id', '未知')}\n"
            f"结束时间: {timestamp or '未知'}\n"
            f"错误: {message or 'Selected model is at capacity.'}"
            + suffix
        )

    async def _notify(self, text: str) -> None:
        if not self.target_umo:
            logger.error("%s target_umo is empty", PLUGIN_NAME)
            return
        sent = await self.context.send_message(
            self.target_umo,
            MessageChain().message(text),
        )
        if not sent:
            raise RuntimeError(f"No active platform matched {self.target_umo}")
        logger.info("%s notification sent to %s", PLUGIN_NAME, self.target_umo)

    @filter.command("codex监控状态")
    async def monitor_status(self, event: AstrMessageEvent):
        yield event.plain_result(
            "Codex 监控状态："
            f"enabled={self.enabled}，"
            f"active_turns={len(self.active_turns)}，"
            f"tracked_files={len(self.offsets)}，"
            f"target={self.target_umo}"
        )

    @filter.command("codex自动继续", alias={"codex继续开关"})
    async def toggle_auto_continue(self, event: AstrMessageEvent, action: str = ""):
        """Toggle unlimited capacity retries from the configured QQ session."""
        if self.target_umo and event.unified_msg_origin != self.target_umo:
            yield event.plain_result("此指令只允许在 Codex 监控配置的 QQ 会话中使用。")
            return

        normalized = action.strip().lower()
        if normalized in {"开", "开启", "on", "true", "1"}:
            self.auto_continue_on_capacity = True
            result = "已开启"
        elif normalized in {"关", "关闭", "off", "false", "0"}:
            self.auto_continue_on_capacity = False
            result = "已关闭"
            for task in tuple(self._pending_retry_tasks.values()):
                task.cancel()
        elif normalized in {"状态", "status"}:
            result = "当前为开启" if self.auto_continue_on_capacity else "当前为关闭"
        elif not normalized:
            self.auto_continue_on_capacity = not self.auto_continue_on_capacity
            result = "已开启" if self.auto_continue_on_capacity else "已关闭"
            if not self.auto_continue_on_capacity:
                for task in tuple(self._pending_retry_tasks.values()):
                    task.cancel()
        else:
            yield event.plain_result("用法：codex自动继续 [开|关|状态]；不带参数时切换开关。")
            return

        state = "开启" if self.auto_continue_on_capacity else "关闭"
        yield event.plain_result(
            f"Codex 容量错误自动继续：{result}（{state}，重试次数无上限）。"
        )

    async def terminate(self) -> None:
        for task in tuple(self._retry_tasks):
            task.cancel()
        if self._retry_tasks:
            await asyncio.gather(*self._retry_tasks, return_exceptions=True)
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._save_state()
