import asyncio
import json
import os
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
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.state_path = self.data_dir / "monitor_state.json"
        self.offsets: dict[str, int] = {}
        self.metadata: dict[str, dict[str, str]] = {}
        self.active_turns: dict[str, ActiveTurn] = {}
        self.notified_event_ids: set[str] = set()
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
                try:
                    self.offsets[key] = path.stat().st_size
                except OSError:
                    continue
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

        if event_type != "event_msg" or not turn_id:
            return
        event_id = f"{payload_type}:{turn_id}"
        if event_id in self.notified_event_ids:
            self.active_turns.pop(turn_id, None)
            return

        if payload_type == "task_complete":
            self.active_turns.pop(turn_id, None)
            last_message = str(payload.get("last_agent_message") or "").strip()
            if not last_message:
                if self.notify_unexpected_stop:
                    self.notified_event_ids.add(event_id)
                    await self._notify(
                        self._format_empty_task_complete(path, payload, timestamp)
                    )
                return
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

    async def terminate(self) -> None:
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
        self._save_state()
