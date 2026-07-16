import asyncio
import datetime as dt
import json
import math
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


PLUGIN_NAME = "zotero_arxiv_digest"
ARXIV_API_URL = "https://export.arxiv.org/api/query"
ZOTERO_API_BASE = "https://api.zotero.org"
ATOM_NS = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}


DEFAULT_CONFIG = {
    "enabled": True,
    "timezone": "Asia/Shanghai",
    "schedule_time": "14:00",
    "zotero": {
        "library_type": "user",
        "user_id": "",
        "group_id": "",
        "api_key": "",
        "collection_keys": [],
        "max_items": 400,
    },
    "arxiv": {
        "categories": ["cs.AI", "cs.CL", "cs.LG", "cs.CV", "stat.ML"],
        "query_extra": "",
        "max_results": 120,
        "days_back": 1,
        "fallback_lookback_days": 5,
    },
    "matching": {
        "max_papers": 8,
        "min_score": 2.0,
        "profile_terms": 80,
    },
    "llm": {
        "provider_id": "",
        "max_prompt_chars": 18000,
    },
    "send": {
        "sessions": [],
        "max_message_chars": 3500,
    },
}


STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "into",
    "is",
    "it",
    "of",
    "on",
    "or",
    "the",
    "to",
    "via",
    "with",
    "without",
    "using",
    "that",
    "this",
    "these",
    "those",
    "their",
    "there",
    "where",
    "which",
    "while",
    "both",
    "can",
    "our",
    "your",
    "they",
    "them",
    "than",
    "then",
    "also",
    "such",
    "more",
    "most",
    "many",
    "some",
    "through",
    "across",
    "over",
    "under",
    "between",
    "within",
    "based",
    "towards",
    "toward",
    "learning",
    "model",
    "models",
    "method",
    "methods",
    "approach",
    "paper",
}


@dataclass
class ZoteroItem:
    key: str
    title: str
    abstract: str
    tags: list[str]
    year: str = ""


@dataclass
class ArxivPaper:
    arxiv_id: str
    title: str
    summary: str
    authors: list[str]
    categories: list[str]
    published: str
    updated: str
    url: str
    score: float = 0.0
    matched_terms: list[str] | None = None


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _load_json(path: Path, default: Any) -> Any:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("[%s] failed to read %s: %s", PLUGIN_NAME, path, exc)
    return default


def _save_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _compact(value: object, limit: int | None = None) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _clean_multiline(value: object, limit: int | None = None) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    cleaned: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if cleaned and not blank:
                cleaned.append("")
            blank = True
            continue
        cleaned.append(line)
        blank = False
    text = "\n".join(cleaned).strip()
    if limit and len(text) > limit:
        return text[: limit - 1].rstrip() + "..."
    return text


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9-]{2,}", text.lower())
    return [w for w in words if w not in STOPWORDS and not w.isdigit()]


def _arxiv_id_from_url(url: str) -> str:
    return url.rstrip("/").split("/")[-1]


def _parse_time(value: str) -> str:
    return value[:10] if value else ""


class ZoteroArxivService:
    def __init__(self, config: dict[str, Any], context: Context):
        self.config = config
        self.context = context

    def missing_config(self) -> list[str]:
        zotero = self.config["zotero"]
        missing: list[str] = []
        if not str(zotero.get("api_key") or "").strip():
            missing.append("zotero.api_key")
        library_type = str(zotero.get("library_type") or "user").strip()
        if library_type == "group":
            if not str(zotero.get("group_id") or "").strip():
                missing.append("zotero.group_id")
        elif not str(zotero.get("user_id") or "").strip():
            missing.append("zotero.user_id")
        return missing

    async def fetch_zotero_items(self) -> list[ZoteroItem]:
        zotero = self.config["zotero"]
        library_type = str(zotero.get("library_type") or "user").strip()
        library_id = str(zotero.get("group_id") if library_type == "group" else zotero.get("user_id")).strip()
        if not library_id:
            raise ValueError("Zotero library id is empty")

        collection_keys = [str(x).strip() for x in zotero.get("collection_keys") or [] if str(x).strip()]
        headers = {
            "Zotero-API-Key": str(zotero.get("api_key") or "").strip(),
            "Accept": "application/json",
            "User-Agent": "AstrBot-Zotero-arXiv-Digest/0.1",
        }
        max_items = int(zotero.get("max_items") or 400)
        items: dict[str, ZoteroItem] = {}
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
            if collection_keys:
                collection_keys = await self._expand_collection_keys(session, library_type, library_id, collection_keys)
                paths = [f"/{library_type}s/{library_id}/collections/{key}/items" for key in collection_keys]
            else:
                paths = [f"/{library_type}s/{library_id}/items"]
            for path in paths:
                start = 0
                while len(items) < max_items:
                    params = {
                        "format": "json",
                        "include": "data",
                        "itemType": "-attachment",
                        "limit": "100",
                        "start": str(start),
                        "sort": "dateModified",
                        "direction": "desc",
                    }
                    url = f"{ZOTERO_API_BASE}{path}?{urlencode(params)}"
                    payload = await self._get_json(session, url)
                    if not isinstance(payload, list) or not payload:
                        break
                    for item in payload:
                        parsed = self._parse_zotero_item(item)
                        if parsed and parsed.title:
                            items[parsed.key] = parsed
                    if len(payload) < 100:
                        break
                    start += 100
        return list(items.values())[:max_items]

    async def _expand_collection_keys(
        self,
        session: aiohttp.ClientSession,
        library_type: str,
        library_id: str,
        roots: list[str],
    ) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []

        async def visit(collection_key: str) -> None:
            if collection_key in seen:
                return
            seen.add(collection_key)
            ordered.append(collection_key)
            start = 0
            while True:
                params = {
                    "format": "json",
                    "limit": "100",
                    "start": str(start),
                }
                url = (
                    f"{ZOTERO_API_BASE}/{library_type}s/{library_id}/collections/"
                    f"{collection_key}/collections?{urlencode(params)}"
                )
                payload = await self._get_json(session, url)
                if not isinstance(payload, list) or not payload:
                    break
                for item in payload:
                    data = item.get("data") if isinstance(item, dict) else None
                    child_key = str((data or {}).get("key") or "").strip()
                    if child_key:
                        await visit(child_key)
                if len(payload) < 100:
                    break
                start += 100

        for root in roots:
            await visit(root)
        return ordered

    def _parse_zotero_item(self, item: dict[str, Any]) -> ZoteroItem | None:
        data = item.get("data") if isinstance(item, dict) else None
        if not isinstance(data, dict):
            return None
        item_type = str(data.get("itemType") or "")
        if item_type in {"attachment", "note"}:
            return None
        tags = []
        for tag in data.get("tags") or []:
            if isinstance(tag, dict) and tag.get("tag"):
                tags.append(str(tag["tag"]))
        return ZoteroItem(
            key=str(data.get("key") or item.get("key") or ""),
            title=_compact(data.get("title")),
            abstract=_compact(data.get("abstractNote")),
            tags=tags,
            year=str(data.get("date") or "")[:4],
        )

    async def fetch_arxiv_yesterday(self, target_date: dt.date | None = None) -> list[ArxivPaper]:
        tz = ZoneInfo(str(self.config.get("timezone") or "Asia/Shanghai"))
        if target_date is None:
            target_date = (dt.datetime.now(tz).date() - dt.timedelta(days=int(self.config["arxiv"].get("days_back", 1))))

        ymd = target_date.strftime("%Y%m%d")
        categories = [str(x).strip() for x in self.config["arxiv"].get("categories") or [] if str(x).strip()]
        cat_query = " OR ".join(f"cat:{cat}" for cat in categories) if categories else "all:*"
        date_query = f"submittedDate:[{ymd}0000 TO {ymd}2359]"
        query_extra = str(self.config["arxiv"].get("query_extra") or "").strip()
        search_query = f"({cat_query}) AND {date_query}"
        if query_extra:
            search_query = f"({search_query}) AND ({query_extra})"

        params = {
            "search_query": search_query,
            "start": "0",
            "max_results": str(int(self.config["arxiv"].get("max_results") or 120)),
            "sortBy": "submittedDate",
            "sortOrder": "descending",
        }
        timeout = aiohttp.ClientTimeout(total=30)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            text = ""
            for attempt in range(3):
                async with session.get(ARXIV_API_URL, params=params, headers={"User-Agent": "AstrBot-Zotero-arXiv-Digest/0.1"}) as resp:
                    text = await resp.text()
                    if resp.status == 429 and attempt < 2:
                        retry_after = resp.headers.get("Retry-After")
                        wait_seconds = int(retry_after) if retry_after and retry_after.isdigit() else 20 * (attempt + 1)
                        await asyncio.sleep(wait_seconds)
                        continue
                    if resp.status >= 400:
                        raise ValueError(f"arXiv API HTTP {resp.status}: {text[:160]}")
                    break
        return self._parse_arxiv_feed(text)

    def _parse_arxiv_feed(self, text: str) -> list[ArxivPaper]:
        root = ET.fromstring(text)
        papers: list[ArxivPaper] = []
        for entry in root.findall("a:entry", ATOM_NS):
            url = _compact(entry.findtext("a:id", default="", namespaces=ATOM_NS))
            categories = [
                str(cat.attrib.get("term"))
                for cat in entry.findall("a:category", ATOM_NS)
                if cat.attrib.get("term")
            ]
            papers.append(
                ArxivPaper(
                    arxiv_id=_arxiv_id_from_url(url),
                    title=_compact(entry.findtext("a:title", default="", namespaces=ATOM_NS)),
                    summary=_compact(entry.findtext("a:summary", default="", namespaces=ATOM_NS)),
                    authors=[
                        _compact(author.findtext("a:name", default="", namespaces=ATOM_NS))
                        for author in entry.findall("a:author", ATOM_NS)
                    ],
                    categories=categories,
                    published=_parse_time(entry.findtext("a:published", default="", namespaces=ATOM_NS)),
                    updated=_parse_time(entry.findtext("a:updated", default="", namespaces=ATOM_NS)),
                    url=url,
                )
            )
        return papers

    def build_profile(self, items: list[ZoteroItem]) -> tuple[list[str], str]:
        counts: dict[str, float] = {}
        for item in items:
            for tag in item.tags:
                tag_norm = tag.lower().strip()
                if tag_norm:
                    counts[tag_norm] = counts.get(tag_norm, 0.0) + 4.0
            for token in _tokens(f"{item.title} {item.abstract}"):
                counts[token] = counts.get(token, 0.0) + 1.0
        terms = [
            term
            for term, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[
                : int(self.config["matching"].get("profile_terms") or 80)
            ]
        ]
        sample_titles = "\n".join(f"- {item.title}" for item in items[:30])
        return terms, sample_titles

    def rank_papers(self, papers: list[ArxivPaper], profile_terms: list[str]) -> list[ArxivPaper]:
        term_set = set(profile_terms)
        max_papers = int(self.config["matching"].get("max_papers") or 8)
        min_score = float(self.config["matching"].get("min_score") or 2.0)

        ranked: list[ArxivPaper] = []
        for paper in papers:
            text = f"{paper.title} {paper.summary}".lower()
            paper_tokens = set(_tokens(text))
            matched: list[str] = []
            score = 0.0
            for term in term_set:
                if " " in term:
                    if term in text:
                        score += 4.0
                        matched.append(term)
                elif term in paper_tokens:
                    score += 1.0 + min(1.5, math.log1p(len(term)) / 2)
                    matched.append(term)
            if paper.categories:
                score += 0.25
            if score >= min_score:
                paper.score = round(score, 2)
                paper.matched_terms = matched[:8]
                ranked.append(paper)
        return sorted(ranked, key=lambda p: (-p.score, p.published, p.title))[:max_papers]

    async def build_digest(self, *, force_date: dt.date | None = None) -> str:
        missing = self.missing_config()
        if missing:
            return "Zotero arXiv 日报未配置完成：缺少 " + ", ".join(missing)

        logger.info("[%s] fetching Zotero items", PLUGIN_NAME)
        zotero_items = await asyncio.wait_for(self.fetch_zotero_items(), timeout=90)
        if not zotero_items:
            return "Zotero arXiv 日报：没有从 Zotero 拉取到可用论文条目。"
        profile_terms, sample_titles = self.build_profile(zotero_items)
        tz = ZoneInfo(str(self.config.get("timezone") or "Asia/Shanghai"))
        target_date = force_date or (
            dt.datetime.now(tz).date() - dt.timedelta(days=int(self.config["arxiv"].get("days_back", 1)))
        )
        logger.info("[%s] fetched %s Zotero items; querying arXiv for %s", PLUGIN_NAME, len(zotero_items), target_date)
        arxiv_papers = await asyncio.wait_for(self.fetch_arxiv_yesterday(target_date), timeout=150)
        if not arxiv_papers and force_date is None:
            max_lookback = max(0, int(self.config["arxiv"].get("fallback_lookback_days", 5)))
            for offset in range(1, max_lookback + 1):
                fallback_date = target_date - dt.timedelta(days=offset)
                logger.info("[%s] no arXiv papers on %s; trying %s", PLUGIN_NAME, target_date, fallback_date)
                await asyncio.sleep(3)
                fallback_papers = await asyncio.wait_for(self.fetch_arxiv_yesterday(fallback_date), timeout=150)
                if fallback_papers:
                    target_date = fallback_date
                    arxiv_papers = fallback_papers
                    break
        ranked = self.rank_papers(arxiv_papers, profile_terms)
        logger.info(
            "[%s] fetched %s arXiv papers for %s; ranked %s papers",
            PLUGIN_NAME,
            len(arxiv_papers),
            target_date,
            len(ranked),
        )

        if not ranked:
            return (
                f"Zotero arXiv 日报（{target_date.isoformat()}）\n"
                f"已检查 arXiv 新挂论文 {len(arxiv_papers)} 篇；按当前 Zotero 文献画像未找到足够相关的新论文。"
            )

        prompt = self._build_llm_prompt(target_date, zotero_items, sample_titles, profile_terms, ranked)
        logger.info("[%s] summarizing ranked papers with LLM", PLUGIN_NAME)
        summary = await asyncio.wait_for(self._llm_summarize(prompt), timeout=240)
        if summary:
            return summary
        return self._fallback_digest(target_date, ranked, len(zotero_items), len(arxiv_papers))

    def _build_llm_prompt(
        self,
        target_date: dt.date,
        zotero_items: list[ZoteroItem],
        sample_titles: str,
        profile_terms: list[str],
        papers: list[ArxivPaper],
    ) -> str:
        paper_blocks = []
        for idx, paper in enumerate(papers, 1):
            paper_blocks.append(
                "\n".join(
                    [
                        f"[{idx}] {paper.title}",
                        f"Authors: {', '.join(paper.authors[:6])}",
                        f"Categories: {', '.join(paper.categories)}",
                        f"URL: {paper.url}",
                        f"Relevance score: {paper.score}; matched terms: {', '.join(paper.matched_terms or [])}",
                        f"Abstract: {paper.summary}",
                    ]
                )
            )
        prompt = f"""
你是研究助理。请根据用户 Zotero 文献库画像，总结 arXiv 在 {target_date.isoformat()} 新挂出的相关论文。

Zotero 文献库规模：{len(zotero_items)} 篇
高频兴趣词：{", ".join(profile_terms[:50])}
代表性已有论文标题：
{sample_titles}

候选新论文：
{chr(10).join(paper_blocks)}

输出要求：
1. 使用中文。
2. 开头给一句总体判断。
3. 每篇论文用清晰换行展示：标题一行、为什么相关一行、核心贡献一行、建议一行、链接一行。
4. 不要编造摘要外的信息；不确定就明确说可能相关。
5. 每篇论文之间保留一个空行。
6. 控制在 1800 中文字以内，适合 QQ 消息阅读。
""".strip()
        max_chars = int(self.config["llm"].get("max_prompt_chars") or 18000)
        return prompt[:max_chars]

    async def _llm_summarize(self, prompt: str) -> str:
        provider_id = str(self.config["llm"].get("provider_id") or "").strip()
        if not provider_id:
            provider = self.context.get_using_provider()
            provider_id = str(getattr(provider, "provider_config", {}).get("id") or "") if provider else ""
        if not provider_id:
            logger.warning("[%s] no LLM provider available; use fallback digest", PLUGIN_NAME)
            return ""
        try:
            resp = await self.context.llm_generate(
                chat_provider_id=provider_id,
                prompt=prompt,
                system_prompt="你是严谨的中文科研助理，只基于给定论文元数据总结。",
            )
            return _clean_multiline(getattr(resp, "completion_text", ""))
        except Exception as exc:
            logger.warning("[%s] LLM summarize failed: %s", PLUGIN_NAME, exc)
            return ""

    def _fallback_digest(
        self,
        target_date: dt.date,
        papers: list[ArxivPaper],
        zotero_count: int,
        arxiv_count: int,
    ) -> str:
        lines = [
            f"Zotero arXiv 日报（{target_date.isoformat()}）",
            f"Zotero 文献：{zotero_count} 篇；arXiv 新挂候选：{arxiv_count} 篇；匹配：{len(papers)} 篇",
        ]
        for idx, paper in enumerate(papers, 1):
            lines.extend(
                [
                    "",
                    f"{idx}. {paper.title}",
                    f"相关词：{', '.join(paper.matched_terms or [])}",
                    f"分类：{', '.join(paper.categories)}",
                    f"链接：{paper.url}",
                    f"摘要：{_compact(paper.summary, 320)}",
                ]
            )
        return "\n".join(lines)

    async def _get_json(self, session: aiohttp.ClientSession, url: str) -> Any:
        async with session.get(url) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ValueError(f"HTTP {resp.status}: {text[:160]}")
            return json.loads(text)


@register(PLUGIN_NAME, "Codex", "Daily Zotero-informed arXiv digest", "0.1.0")
class ZoteroArxivDigest(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.config_path = self.data_dir / "config.json"
        self.state_path = self.data_dir / "state.json"
        self.config = self._load_config()
        self.state = self._load_state()
        self.service = ZoteroArxivService(self.config, context)
        self._task: asyncio.Task | None = None
        self._run_lock = asyncio.Lock()

    def _load_config(self) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        existing = _load_json(self.config_path, {})
        config = _deep_merge(DEFAULT_CONFIG, existing if isinstance(existing, dict) else {})
        saved = json.loads(json.dumps(config))
        saved["zotero"]["api_key"] = str(existing.get("zotero", {}).get("api_key", ""))
        _save_json(self.config_path, saved)
        return config

    def _load_state(self) -> dict[str, Any]:
        state = _load_json(self.state_path, {})
        if not isinstance(state, dict):
            state = {}
        state.setdefault("last_run_date", "")
        state.setdefault("last_run_at", 0)
        state.setdefault("last_error", "")
        state.setdefault("last_digest_preview", "")
        return state

    def _save_state(self) -> None:
        _save_json(self.state_path, self.state)

    async def initialize(self) -> None:
        self._maybe_import_existing_session()
        if self.config.get("enabled", True):
            self._task = asyncio.create_task(self._scheduler_loop())
            logger.info("[%s] scheduler loop started", PLUGIN_NAME)

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _maybe_import_existing_session(self) -> None:
        sessions = self.config.setdefault("send", {}).setdefault("sessions", [])
        if sessions:
            return
        balance_state_path = Path(get_astrbot_data_path()) / "plugin_data" / "sub2api_balance_monitor" / "state.json"
        other = _load_json(balance_state_path, {})
        imported = [s for s in other.get("sessions", []) if isinstance(s, str) and s]
        if imported:
            sessions.extend(imported)
            _save_json(self.config_path, self.config)

    async def _scheduler_loop(self) -> None:
        await asyncio.sleep(15)
        while True:
            try:
                if self._should_run_now():
                    missing = self.service.missing_config()
                    if missing:
                        self.state["last_error"] = "missing config: " + ", ".join(missing)
                        self._save_state()
                        await asyncio.sleep(60)
                        continue
                    await self._run_and_send()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.state["last_error"] = str(exc)
                self._save_state()
                logger.warning("[%s] scheduled run failed: %s", PLUGIN_NAME, exc)
            await asyncio.sleep(60)

    def _should_run_now(self) -> bool:
        tz = ZoneInfo(str(self.config.get("timezone") or "Asia/Shanghai"))
        now = dt.datetime.now(tz)
        schedule_time = str(self.config.get("schedule_time") or "14:00")
        hour, minute = [int(x) for x in schedule_time.split(":", 1)]
        today = now.date().isoformat()
        if self.state.get("last_run_date") == today:
            return False
        return (now.hour, now.minute) >= (hour, minute)

    async def _run_and_send(self, *, event: AstrMessageEvent | None = None) -> str:
        try:
            async with self._run_lock:
                digest = await asyncio.wait_for(self.service.build_digest(), timeout=420)
                self.state["last_run_date"] = dt.datetime.now(
                    ZoneInfo(str(self.config.get("timezone") or "Asia/Shanghai"))
                ).date().isoformat()
                self.state["last_run_at"] = int(time.time())
                self.state["last_error"] = ""
                self.state["last_digest_preview"] = digest[:500]
                self._save_state()
            await self._send_digest(digest, event=event)
            logger.info("[%s] digest sent successfully", PLUGIN_NAME)
            return digest
        except Exception as exc:
            message = f"Zotero arXiv 日报生成失败：{exc}"
            self.state["last_error"] = str(exc)
            self.state["last_run_at"] = int(time.time())
            self._save_state()
            logger.warning("[%s] digest run failed: %s", PLUGIN_NAME, exc)
            if event:
                await event.send(MessageChain().message(message))
            raise

    async def _run_manual_task(self, event: AstrMessageEvent) -> None:
        try:
            await self._run_and_send(event=event)
        except Exception:
            pass

    async def _send_digest(self, digest: str, *, event: AstrMessageEvent | None = None) -> None:
        sessions = [s for s in self.config.get("send", {}).get("sessions", []) if isinstance(s, str) and s]
        if event and event.unified_msg_origin not in sessions:
            sessions.append(event.unified_msg_origin)
            self.config["send"]["sessions"] = sessions
            _save_json(self.config_path, self.config)
        if not sessions and event:
            await event.send(MessageChain().message(digest))
            return
        if not sessions:
            raise RuntimeError("no bound sessions for arXiv digest")

        chunks = self._split_message(digest)
        for session in sessions:
            for chunk in chunks:
                await self.context.send_message(session, MessageChain().message(chunk))

    def _split_message(self, text: str) -> list[str]:
        limit = int(self.config.get("send", {}).get("max_message_chars") or 3500)
        if len(text) <= limit:
            return [text]
        chunks: list[str] = []
        remaining = text
        while remaining:
            chunks.append(remaining[:limit])
            remaining = remaining[limit:]
        return chunks

    def _status_text(self) -> str:
        missing = self.service.missing_config()
        sessions = self.config.get("send", {}).get("sessions", [])
        lines = [
            "Zotero arXiv 日报状态",
            f"启用：{bool(self.config.get('enabled', True))}",
            f"时间：{self.config.get('timezone')} {self.config.get('schedule_time')}",
            f"接收会话数：{len(sessions)}",
            f"Zotero 配置：{'缺少 ' + ', '.join(missing) if missing else '已配置'}",
            f"arXiv 分类：{', '.join(self.config.get('arxiv', {}).get('categories') or [])}",
            f"上次运行日期：{self.state.get('last_run_date') or '无'}",
            f"上次错误：{self.state.get('last_error') or '无'}",
            "命令：论文推送 绑定 / 状态 / 检查 / 立即 / 测试",
        ]
        return "\n".join(lines)

    @filter.command("论文推送", alias={"arxiv日报", "zotero_arxiv"})
    async def paper_digest(self, event: AstrMessageEvent, args: GreedyStr = ""):
        action = str(args or "").strip().lower()
        if action in {"绑定", "bind"}:
            sessions = self.config.setdefault("send", {}).setdefault("sessions", [])
            if event.unified_msg_origin not in sessions:
                sessions.append(event.unified_msg_origin)
                _save_json(self.config_path, self.config)
            yield event.plain_result("已绑定当前会话为 Zotero arXiv 日报接收会话。")
            return

        if action in {"状态", "status", ""}:
            yield event.plain_result(self._status_text())
            return

        if action in {"检查", "check"}:
            missing = self.service.missing_config()
            if missing:
                yield event.plain_result("尚不能运行：缺少 " + ", ".join(missing))
                return
            zotero_items = await self.service.fetch_zotero_items()
            papers = await self.service.fetch_arxiv_yesterday()
            terms, _ = self.service.build_profile(zotero_items)
            ranked = self.service.rank_papers(papers, terms)
            yield event.plain_result(
                f"检查完成：Zotero {len(zotero_items)} 篇；昨日 arXiv {len(papers)} 篇；匹配 {len(ranked)} 篇。"
            )
            return

        if action in {"立即", "run", "now"}:
            yield event.plain_result("开始生成 Zotero arXiv 日报，完成后会发送到绑定会话。")
            asyncio.create_task(self._run_manual_task(event))
            return

        if action in {"测试", "test"}:
            yield event.plain_result(
                "Zotero arXiv 日报测试\n"
                "如果配置完成，每天 14:00 会根据 Zotero 文献库匹配昨日 arXiv 新论文，并用 AI 生成中文摘要后推送到此会话。\n"
                + self._status_text()
            )
            return

        yield event.plain_result("未知参数。可用：绑定、状态、检查、立即、测试。")
