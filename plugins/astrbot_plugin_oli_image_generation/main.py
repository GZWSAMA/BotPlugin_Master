from __future__ import annotations

import asyncio
import base64
import binascii
import ipaddress
import json
import mimetypes
import socket
import re
from urllib.parse import urlsplit
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import aiohttp
from aiohttp.abc import AbstractResolver

from astrbot.api import llm_tool, logger, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, StarTools


PLUGIN_NAME = "astrbot_plugin_oli_image_generation"
DEFAULT_PLATFORM_ID = "奥利"
DEFAULT_API_BASE = "https://code.omniworldmodel.com/v1"
DEFAULT_MODEL = "gpt-image-2"
DAILY_IMAGE_LIMIT = 10
QUOTA_RESET_REPLY = "奥利找到主人啦，今日的图片绘制次数已经重制啦。"
REFERENCE_IMAGE_PATH = Path(__file__).with_name("assets") / "oli_reference.png"
SELF_REFERENCE_PATTERNS = (
    re.compile(r"你(?:的)?(?:样子|形象|外貌|长相|立绘|角色设定)"),
    re.compile(r"(?:画|绘制|生成|做|制作)(?:一下)?(?:你|奥利)(?:本人|自己)?"),
    re.compile(r"(?:以|按照|根据)(?:你|奥利)(?:本人|自己)?(?:为原型|的样子|的形象)"),
    re.compile(r"(?:你|奥利)(?:也在里面|出现在里面|入镜|出镜|在画里|在图里)"),
    re.compile(r"(?:自己的|自我)(?:样子|形象|画像|立绘)"),
)


class _StaticOriginResolver(AbstractResolver):
    """Resolve only the configured API hostname to its verified origin IP."""

    def __init__(self, hostname: str, origin_ip: str):
        self.hostname = hostname
        self.origin_ip = origin_ip
        self._fallback = aiohttp.resolver.DefaultResolver()

    async def resolve(
        self, host: str, port: int = 0, family: int = socket.AF_UNSPEC
    ) -> list[dict[str, Any]]:
        if host != self.hostname:
            # Image URLs may be served from a separate, signed CDN hostname.
            return await self._fallback.resolve(host, port, family)
        address = ipaddress.ip_address(self.origin_ip)
        resolved_family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
        return [
            {
                "hostname": host,
                "host": self.origin_ip,
                "port": port,
                "family": resolved_family,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        await self._fallback.close()


class ImageGenerationError(RuntimeError):
    """Raised when the image API cannot produce an image."""


@star.register(
    PLUGIN_NAME,
    "local",
    "OpenAI-compatible image generation for Oli.",
    "0.1.0",
)
class OliImageGenerationPlugin(Star):
    """Generate and send images for the Oli platform."""

    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        config = config or {}
        self.enabled = bool(config.get("enabled", True))
        self.platform_id = str(
            config.get("platform_id") or DEFAULT_PLATFORM_ID
        ).strip()
        self.api_base = str(
            config.get("api_base") or DEFAULT_API_BASE
        ).strip().rstrip("/")
        self.origin_ip = str(config.get("origin_ip") or "").strip()
        self.api_key = str(config.get("api_key") or "").strip()
        self.model = str(config.get("model") or DEFAULT_MODEL).strip()
        self.edit_model = str(config.get("edit_model") or self.model).strip()
        self.size = str(config.get("size") or "1024x1024").strip()
        self.timeout = max(30, min(int(config.get("timeout", 300)), 600))
        self.data_dir = StarTools.get_data_dir(PLUGIN_NAME)
        self.quota_state_path = self.data_dir / "daily_quota.json"
        self._quota_lock = asyncio.Lock()
        self._quota_day, self._quota_count = self._load_quota_state()
        self.output_dir = self.data_dir / "generated"
        self.output_dir.mkdir(parents=True, exist_ok=True)

    def _check_available(self, event: AstrMessageEvent) -> str | None:
        if not self.enabled:
            return "奥利绘图功能当前未启用。"
        if event.get_platform_id() != self.platform_id:
            return "这个绘图接口只对奥利开放。"
        if not self.api_key:
            return "奥利绘图接口未配置 API key。"
        if not self.api_base:
            return "奥利绘图接口未配置 API base URL。"
        if not self.model:
            return "奥利绘图接口未配置模型。"
        return None

    @staticmethod
    def _prompt_requests_self_reference(prompt: str) -> bool:
        """Return whether the prompt asks to include Oli's own appearance."""
        normalized = re.sub(r"\s+", "", prompt).lower()
        return any(pattern.search(normalized) for pattern in SELF_REFERENCE_PATTERNS)

    @staticmethod
    def _self_reference_image() -> Image | None:
        if not REFERENCE_IMAGE_PATH.is_file():
            logger.warning(
                "%s reference image is missing: %s",
                PLUGIN_NAME,
                REFERENCE_IMAGE_PATH,
            )
            return None
        return Image.fromFileSystem(str(REFERENCE_IMAGE_PATH))

    def _create_session(self, timeout: aiohttp.ClientTimeout) -> aiohttp.ClientSession:
        """Keep HTTPS hostname validation while bypassing a CDN DNS endpoint."""
        if not self.origin_ip:
            return aiohttp.ClientSession(timeout=timeout)

        parsed = urlsplit(self.api_base)
        hostname = parsed.hostname
        if parsed.scheme != "https" or not hostname:
            raise ImageGenerationError(
                "配置 origin_ip 时，API base URL 必须是有效的 HTTPS 域名。"
            )
        try:
            ipaddress.ip_address(self.origin_ip)
        except ValueError as exc:
            raise ImageGenerationError("origin_ip 必须是有效的 IPv4 或 IPv6 地址。") from exc

        connector = aiohttp.TCPConnector(
            resolver=_StaticOriginResolver(hostname, self.origin_ip)
        )
        return aiohttp.ClientSession(timeout=timeout, connector=connector)

    @staticmethod
    def _current_day() -> str:
        return datetime.now().date().isoformat()

    def _load_quota_state(self) -> tuple[str, int]:
        current_day = self._current_day()
        if not self.quota_state_path.exists():
            return current_day, 0
        try:
            payload = json.loads(self.quota_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.exception("%s failed to load daily image quota", PLUGIN_NAME)
            return current_day, DAILY_IMAGE_LIMIT

        if not isinstance(payload, dict) or payload.get("day") != current_day:
            return current_day, 0
        try:
            count = int(payload.get("count", 0))
        except (TypeError, ValueError):
            count = DAILY_IMAGE_LIMIT
        return current_day, min(max(count, 0), DAILY_IMAGE_LIMIT)

    def _save_quota_state(self) -> None:
        payload = {
            "day": self._quota_day,
            "count": self._quota_count,
            "limit": DAILY_IMAGE_LIMIT,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        temporary_path = self.quota_state_path.with_suffix(".json.tmp")
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary_path.replace(self.quota_state_path)

    @staticmethod
    def _quota_reset_signal(text: str) -> bool:
        """Match the owner signal using both Chinese and ASCII punctuation."""
        return (
            sum(text.count(mark) for mark in (",", "，")) >= 5
            and sum(text.count(mark) for mark in (".", "。")) >= 2
            and sum(text.count(mark) for mark in ("!", "！")) >= 1
        )

    async def _reset_daily_quota(self) -> bool:
        current_day = self._current_day()
        async with self._quota_lock:
            previous_day = self._quota_day
            previous_count = self._quota_count
            self._quota_day = current_day
            self._quota_count = 0
            try:
                self._save_quota_state()
            except OSError:
                self._quota_day = previous_day
                self._quota_count = previous_count
                logger.exception("%s failed to reset daily image quota", PLUGIN_NAME)
                return False
        return True

    @filter.event_message_type(filter.EventMessageType.ALL, priority=1000)
    async def reset_quota_on_owner_signal(self, event: AstrMessageEvent) -> None:
        if not self.enabled or event.get_platform_id() != self.platform_id:
            return
        text = event.get_message_str() or ""
        if not self._quota_reset_signal(text):
            return
        if await self._reset_daily_quota():
            event.set_result(event.plain_result(QUOTA_RESET_REPLY))
        else:
            event.set_result(event.plain_result("奥利重制今日绘图次数失败，请稍后重试。"))
        event.stop_event()

    async def _reserve_daily_quota(self) -> str:
        current_day = self._current_day()
        async with self._quota_lock:
            previous_day = self._quota_day
            previous_count = self._quota_count
            if self._quota_day != current_day:
                self._quota_day = current_day
                self._quota_count = 0
            if self._quota_count >= DAILY_IMAGE_LIMIT:
                raise ImageGenerationError(
                    f"今日图片生成次数已达上限（{DAILY_IMAGE_LIMIT} 张），请明天再试。"
                )

            self._quota_count += 1
            try:
                self._save_quota_state()
            except OSError as exc:
                self._quota_day = previous_day
                self._quota_count = previous_count
                raise ImageGenerationError(
                    "无法保存今日绘图额度，请稍后重试。"
                ) from exc
            return current_day

    async def _release_daily_quota(self, reservation_day: str) -> None:
        async with self._quota_lock:
            if (
                reservation_day != self._current_day()
                or self._quota_day != reservation_day
            ):
                return
            self._quota_count = max(0, self._quota_count - 1)
            try:
                self._save_quota_state()
            except OSError:
                logger.exception("%s failed to release daily image quota", PLUGIN_NAME)

    async def _remaining_daily_quota(self) -> int:
        current_day = self._current_day()
        async with self._quota_lock:
            if self._quota_day != current_day:
                self._quota_day = current_day
                self._quota_count = 0
            return max(0, DAILY_IMAGE_LIMIT - self._quota_count)

    @staticmethod
    def _api_error_text(status: int, body: str) -> str:
        detail = body.strip()
        try:
            payload = json.loads(detail)
        except (TypeError, json.JSONDecodeError):
            payload = None
        if isinstance(payload, dict):
            error = payload.get("error")
            if isinstance(error, dict):
                detail = str(error.get("message") or error.get("code") or detail)
            elif error:
                detail = str(error)
        detail = " ".join(detail.split())[:300]
        return f"绘图 API 请求失败（HTTP {status}）: {detail or 'unknown error'}"

    async def _save_image(
        self,
        event: AstrMessageEvent,
        content: bytes,
        content_type: str = "image/png",
    ) -> Path:
        if not content:
            raise ImageGenerationError("绘图 API 返回了空图片。")
        extension = mimetypes.guess_extension(content_type.split(";", 1)[0]) or ".png"
        path = self.output_dir / f"image_{uuid.uuid4().hex}{extension}"
        path.write_bytes(content)
        event.track_temporary_local_file(str(path))
        return path

    async def _download_image(
        self,
        session: aiohttp.ClientSession,
        event: AstrMessageEvent,
        url: str,
    ) -> Path:
        if not url.startswith(("http://", "https://")):
            raise ImageGenerationError("绘图 API 返回了无效图片地址。")
        async with session.get(url) as response:
            if response.status != 200:
                body = await response.text()
                raise ImageGenerationError(
                    self._api_error_text(response.status, body)
                )
            return await self._save_image(
                event,
                await response.read(),
                response.headers.get("Content-Type", "image/png"),
            )

    async def _generate_image(
        self,
        event: AstrMessageEvent,
        prompt: str,
        source_image: Image | None = None,
    ) -> Path:
        prompt = prompt.strip()
        if not prompt:
            raise ImageGenerationError("绘图描述不能为空。")
        if len(prompt) > 4000:
            raise ImageGenerationError("绘图描述不能超过 4000 个字符。")

        reservation_day = await self._reserve_daily_quota()
        try:
            timeout = aiohttp.ClientTimeout(total=self.timeout)
            headers = {"Authorization": f"Bearer {self.api_key}"}
            payload = {
                "model": self.edit_model if source_image else self.model,
                "prompt": prompt,
                "n": 1,
                "size": self.size,
            }
            endpoint = f"{self.api_base}/images/{'edits' if source_image else 'generations'}"
            request_kwargs: dict[str, Any] = {"headers": headers}
            if source_image is None:
                request_kwargs["json"] = payload
                headers["Content-Type"] = "application/json"
            else:
                try:
                    source_path = await source_image.convert_to_file_path()
                    source_bytes = Path(source_path).read_bytes()
                except (OSError, ValueError) as exc:
                    raise ImageGenerationError("无法读取待编辑的图片。") from exc
                if not source_bytes:
                    raise ImageGenerationError("待编辑的图片为空。")
                form = aiohttp.FormData()
                for key, value in payload.items():
                    form.add_field(key, str(value))
                content_type = mimetypes.guess_type(source_path)[0] or "image/png"
                form.add_field(
                    "image",
                    source_bytes,
                    filename=Path(source_path).name or "source.png",
                    content_type=content_type,
                )
                request_kwargs["data"] = form
            async with self._create_session(timeout) as session:
                async with session.post(
                    endpoint,
                    **request_kwargs,
                ) as response:
                    if response.status < 200 or response.status >= 300:
                        body = await response.text()
                        raise ImageGenerationError(
                            self._api_error_text(response.status, body)
                        )
                    response_payload: Any = await response.json(content_type=None)
                data = (
                    response_payload.get("data")
                    if isinstance(response_payload, dict)
                    else None
                )
                if not isinstance(data, list) or not data:
                    raise ImageGenerationError("绘图 API 返回中没有图片数据。")
                image = data[0]
                if not isinstance(image, dict):
                    raise ImageGenerationError("绘图 API 返回了无法识别的图片数据。")

                image_url = image.get("url")
                if isinstance(image_url, str) and image_url:
                    return await self._download_image(session, event, image_url)

                encoded = image.get("b64_json")
                if not isinstance(encoded, str) or not encoded:
                    raise ImageGenerationError(
                        "绘图 API 未返回图片 URL 或 base64 数据。"
                    )
                if encoded.startswith("data:") and "," in encoded:
                    encoded = encoded.split(",", 1)[1]
                try:
                    content = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error) as exc:
                    raise ImageGenerationError(
                        "绘图 API 返回的 base64 图片无效。"
                    ) from exc
                return await self._save_image(event, content)
        except aiohttp.ClientError as exc:
            await self._release_daily_quota(reservation_day)
            raise ImageGenerationError(f"绘图 API 网络请求失败: {exc}") from exc
        except BaseException:
            await self._release_daily_quota(reservation_day)
            raise

    @llm_tool("oli_generate_image")
    async def generate_image(self, event: AstrMessageEvent, prompt: str):
        """为奥利生成一张图片，并直接发送给用户。

        Args:
            prompt(string): 要生成的图片描述，应该具体说明主体、场景和风格。
        """
        unavailable = self._check_available(event)
        if unavailable:
            return unavailable
        try:
            reference = (
                self._self_reference_image()
                if self._prompt_requests_self_reference(prompt)
                else None
            )
            path = await self._generate_image(event, prompt, reference)
        except ImageGenerationError as exc:
            logger.warning("%s image generation failed: %s", PLUGIN_NAME, exc)
            return str(exc)
        remaining = await self._remaining_daily_quota()
        return event.make_result().file_image(str(path)).message(
            f"今日剩余图片生成次数：{remaining} 张"
        )

    @staticmethod
    def _find_image(event: AstrMessageEvent) -> Image | None:
        """Return the first image in the incoming chain, including a quoted chain."""
        def walk(components):
            for component in components or []:
                if isinstance(component, Image):
                    return component
                nested = getattr(component, "chain", None)
                found = walk(nested)
                if found:
                    return found
            return None

        return walk(event.get_messages())

    @llm_tool("oli_edit_image")
    async def edit_image(self, event: AstrMessageEvent, prompt: str):
        """使用当前消息中的图片，按描述生成一张修改后的图片。"""
        unavailable = self._check_available(event)
        if unavailable:
            return unavailable
        source_image = self._find_image(event)
        if source_image is None:
            return "请先发送一张图片，再描述需要如何修改。"
        try:
            path = await self._generate_image(event, prompt, source_image)
        except ImageGenerationError as exc:
            logger.warning("%s image edit failed: %s", PLUGIN_NAME, exc)
            return str(exc)
        remaining = await self._remaining_daily_quota()
        return event.make_result().file_image(str(path)).message(
            f"今日剩余图片生成次数：{remaining} 张"
        )

    @filter.command("画图")
    @filter.command("绘图")
    async def draw_command(self, event: AstrMessageEvent):
        """Generate an image from an explicit command."""
        unavailable = self._check_available(event)
        if unavailable:
            yield event.plain_result(unavailable)
            return

        message = event.get_message_str().strip()
        prompt = message
        for command in ("画图", "绘图"):
            if prompt.startswith(command):
                prompt = prompt[len(command) :].strip()
                break
        if not prompt:
            yield event.plain_result("用法：画图 <图片描述>")
            return

        try:
            reference = (
                self._self_reference_image()
                if self._prompt_requests_self_reference(prompt)
                else None
            )
            path = await self._generate_image(event, prompt, reference)
        except ImageGenerationError as exc:
            logger.warning("%s command generation failed: %s", PLUGIN_NAME, exc)
            yield event.plain_result(str(exc))
            return
        remaining = await self._remaining_daily_quota()
        yield event.make_result().file_image(str(path)).message(
            f"今日剩余图片生成次数：{remaining} 张"
        )

    @filter.command("图生图")
    @filter.command("改图")
    async def image_to_image_command(self, event: AstrMessageEvent):
        """Edit the first image in the message chain using a text instruction."""
        unavailable = self._check_available(event)
        if unavailable:
            yield event.plain_result(unavailable)
            return
        source_image = self._find_image(event)
        if source_image is None:
            yield event.plain_result("用法：发送一张图片并附带“图生图 <修改描述>”。")
            return
        message = event.get_message_str().strip()
        prompt = message
        for command in ("图生图", "改图"):
            if prompt.startswith(command):
                prompt = prompt[len(command) :].strip()
                break
        if not prompt:
            yield event.plain_result("用法：图生图 <修改描述>（需同时发送原图）")
            return
        try:
            path = await self._generate_image(event, prompt, source_image)
        except ImageGenerationError as exc:
            logger.warning("%s command image edit failed: %s", PLUGIN_NAME, exc)
            yield event.plain_result(str(exc))
            return
        remaining = await self._remaining_daily_quota()
        yield event.make_result().file_image(str(path)).message(
            f"今日剩余图片生成次数：{remaining} 张"
        )
