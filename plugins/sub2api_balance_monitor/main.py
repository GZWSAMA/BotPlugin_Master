import asyncio
import datetime
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import aiohttp
import asyncpg

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.star.filter.command import GreedyStr
from astrbot.core.utils.astrbot_path import get_astrbot_data_path


PLUGIN_NAME = "sub2api_balance_monitor"
ACCOUNT_NAME_RE = re.compile(r"^[^-]+-.+$")
DEFAULT_RECHARGE_URL = ""


DEFAULT_CONFIG = {
    "enabled": True,
    "check_interval_seconds": 600,
    "alert_repeat_seconds": 7200,
    "threshold": 2.0,
    "recovered_threshold": 2.5,
    "recharge_url": DEFAULT_RECHARGE_URL,
    "recharge_urls": {},
    "http_timeout_seconds": 10,
    "db": {
        "host": os.getenv("SUB2API_DB_HOST", "postgres"),
        "port": int(os.getenv("SUB2API_DB_PORT", "5432")),
        "user": os.getenv("SUB2API_DB_USER", "sub2api"),
        "password": os.getenv("SUB2API_DB_PASSWORD", ""),
        "database": os.getenv("SUB2API_DB_NAME", "sub2api"),
    },
    "newapi_token_upstreams": ["fishxcode"],
    "skip_upstreams": ["cctq", "codelife", "joverna"],
    "rate_monitor_enabled": True,
    "ratio_watch": {
        "site_types": {},
        "newapi_auth": {},
        "sub2api_auth": {},
    },
}


@dataclass
class Account:
    id: int
    name: str
    upstream: str
    actual_group: str
    platform: str
    base_url: str
    api_key: str
    groups: str = ""
    notes: str = ""


@dataclass
class BalanceProbe:
    account: Account
    adapter: str
    ok: bool
    balance: float | None = None
    url: str = ""
    skipped: bool = False
    error: str = ""


@dataclass
class ChannelReport:
    upstream: str
    accounts: list[Account]
    probes: list[BalanceProbe] = field(default_factory=list)

    @property
    def successful_balances(self) -> list[float]:
        return [p.balance for p in self.probes if p.ok and p.balance is not None]

    @property
    def balance(self) -> float | None:
        balances = self.successful_balances
        if not balances:
            return None
        return min(balances)

    @property
    def skipped(self) -> bool:
        return self.probes and all(p.skipped for p in self.probes)


@dataclass
class GroupRateReport:
    upstream: str
    root_url: str
    accounts: list[Account]
    ok: bool
    skipped: bool = False
    source: str = ""
    platform: str = ""
    groups: dict[str, dict[str, Any]] = field(default_factory=dict)
    rates: dict[str, float] = field(default_factory=dict)
    url: str = ""
    error: str = ""


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


def _normalize_base_url(base_url: str) -> str:
    base_url = (base_url or "").strip()
    if not base_url:
        return ""
    if not base_url.endswith("/"):
        base_url += "/"
    return base_url


def _root_from_base_url(base_url: str) -> str:
    base = _normalize_base_url(base_url)
    if base.rstrip("/").endswith("/v1"):
        return base.rstrip("/")[:-3].rstrip("/") + "/"
    return base


def _display_root_url(base_url: str) -> str:
    return _root_from_base_url(base_url).strip().rstrip("/")


def _usage_url(base_url: str) -> str:
    base = _normalize_base_url(base_url)
    if base.rstrip("/").endswith("/v1"):
        return urljoin(base, "usage")
    return urljoin(base, "v1/usage")


def _newapi_usage_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/usage/token")


def _newapi_pricing_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/pricing")


def _newapi_user_groups_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/user/groups")


def _newapi_user_self_groups_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/user/self/groups")


def _sub2api_groups_available_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/v1/groups/available")


def _sub2api_groups_rates_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/v1/groups/rates")


def _sub2api_login_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/v1/auth/login")


def _sub2api_refresh_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/v1/auth/refresh")


def _newapi_group_url(base_url: str) -> str:
    return urljoin(_root_from_base_url(base_url), "api/group")


def _fmt_money(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:.4f}".rstrip("0").rstrip(".")


def _fmt_rate(value: float | None) -> str:
    if value is None:
        return "未知"
    return f"{value:.4f}".rstrip("0").rstrip(".") + "x"


def _normalize_group_ratio(raw: Any) -> tuple[Any, str]:
    if isinstance(raw, (int, float)):
        return float(raw), "number"
    if isinstance(raw, str):
        stripped = raw.strip()
        try:
            return float(stripped), "number"
        except ValueError:
            return stripped, "text"
    return raw, "text"


class Sub2APIBalanceService:
    def __init__(self, config: dict[str, Any]):
        self.config = config

    async def load_accounts(self) -> list[Account]:
        db = self.config["db"]
        conn = await asyncpg.connect(
            host=db["host"],
            port=int(db["port"]),
            user=db["user"],
            password=db["password"],
            database=db["database"],
            timeout=8,
        )
        try:
            rows = await conn.fetch(
                """
                with active_accounts as (
                    select
                        a.id,
                        a.name,
                        a.platform,
                        coalesce(
                            nullif(a.credentials->>'base_url', ''),
                            nullif(a.credentials->>'api_base', ''),
                            nullif(a.credentials->>'endpoint', ''),
                            nullif(a.credentials->>'url', '')
                        ) as base_url,
                        a.credentials->>'api_key' as api_key,
                        string_agg(g.name, ', ' order by g.name) as groups,
                        a.notes
                    from accounts a
                    left join account_groups ag on ag.account_id = a.id
                    left join groups g on g.id = ag.group_id
                    where a.deleted_at is null
                      and a.status = 'active'
                      and a.schedulable is true
                    group by a.id
                )
                select id, name, platform, base_url, api_key, coalesce(groups, '') as groups, coalesce(notes, '') as notes
                from active_accounts
                where name ~ '^[^-]+-.+$'
                  and coalesce(api_key, '') <> ''
                  and coalesce(base_url, '') <> ''
                order by split_part(name, '-', 1), name
                """
            )
        finally:
            await conn.close()

        accounts: list[Account] = []
        for row in rows:
            name = str(row["name"])
            if not ACCOUNT_NAME_RE.match(name):
                continue
            upstream, actual_group = name.split("-", 1)
            accounts.append(
                Account(
                    id=int(row["id"]),
                    name=name,
                    upstream=upstream,
                    actual_group=actual_group,
                    platform=str(row["platform"] or ""),
                    base_url=str(row["base_url"] or ""),
                    api_key=str(row["api_key"] or ""),
                    groups=str(row["groups"] or ""),
                    notes=str(row["notes"] or ""),
                )
            )
        return accounts

    def group_accounts(self, accounts: list[Account]) -> dict[str, list[Account]]:
        grouped: dict[str, list[Account]] = {}
        for account in accounts:
            grouped.setdefault(account.upstream, []).append(account)
        return grouped

    def adapter_for(self, upstream: str) -> str | None:
        skip = set(self.config.get("skip_upstreams") or [])
        newapi = set(self.config.get("newapi_token_upstreams") or [])
        if upstream in skip:
            return None
        if upstream in newapi:
            return "newapi_token"
        return "sub2api_usage"

    async def probe_account(
        self,
        session: aiohttp.ClientSession,
        account: Account,
    ) -> BalanceProbe:
        adapter = self.adapter_for(account.upstream)
        if adapter is None:
            return BalanceProbe(account=account, adapter="skipped", ok=False, skipped=True)
        if adapter == "newapi_token":
            return await self._probe_newapi_token(session, account)
        return await self._probe_sub2api_usage(session, account)

    async def _probe_sub2api_usage(
        self,
        session: aiohttp.ClientSession,
        account: Account,
    ) -> BalanceProbe:
        url = _usage_url(account.base_url)
        try:
            payload = await self._get_json(session, url, account.api_key)
            balance = payload.get("remaining", payload.get("balance"))
            if isinstance(balance, (int, float)):
                return BalanceProbe(
                    account=account,
                    adapter="sub2api_usage",
                    ok=True,
                    balance=float(balance),
                    url=url,
                )
            return BalanceProbe(
                account=account,
                adapter="sub2api_usage",
                ok=False,
                url=url,
                error="response has no remaining/balance field",
            )
        except Exception as exc:
            return BalanceProbe(
                account=account,
                adapter="sub2api_usage",
                ok=False,
                url=url,
                error=str(exc),
            )

    def _extract_sub2api_usage_rate(self, payload: dict[str, Any]) -> float | None:
        rows = payload.get("daily_usage")
        if not isinstance(rows, list):
            rows = payload.get("usage")
        if not isinstance(rows, list):
            return None

        total_cost = 0.0
        total_actual_cost = 0.0
        for row in rows:
            if not isinstance(row, dict):
                continue
            cost = row.get("cost")
            actual_cost = row.get("actual_cost")
            if not isinstance(cost, (int, float)) or not isinstance(actual_cost, (int, float)):
                continue
            if float(cost) <= 0:
                continue
            total_cost += float(cost)
            total_actual_cost += float(actual_cost)

        if total_cost <= 0:
            return None
        return total_actual_cost / total_cost

    async def _probe_newapi_token(
        self,
        session: aiohttp.ClientSession,
        account: Account,
    ) -> BalanceProbe:
        url = _newapi_usage_url(account.base_url)
        try:
            payload = await self._get_json(session, url, account.api_key)
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict):
                raise ValueError("response has no data object")
            if data.get("unlimited_quota") is True:
                raise ValueError("token is unlimited_quota; skip token quota as balance")
            total_available = data.get("total_available")
            if not isinstance(total_available, (int, float)):
                raise ValueError("response has no numeric total_available")
            balance = float(total_available) / 500000.0
            return BalanceProbe(
                account=account,
                adapter="newapi_token",
                ok=True,
                balance=balance,
                url=url,
            )
        except Exception as exc:
            fallback = await self._probe_openai_billing_fallback(session, account, str(exc))
            if fallback.ok:
                return fallback
            return fallback

    async def _probe_openai_billing_fallback(
        self,
        session: aiohttp.ClientSession,
        account: Account,
        first_error: str,
    ) -> BalanceProbe:
        root = _root_from_base_url(account.base_url)
        subscription_url = urljoin(root, "v1/dashboard/billing/subscription")
        today = datetime.date.today().isoformat()
        usage_url = urljoin(
            root,
            f"v1/dashboard/billing/usage?start_date=2020-01-01&end_date={today}",
        )
        try:
            subscription = await self._get_json(session, subscription_url, account.api_key)
            usage = await self._get_json(session, usage_url, account.api_key)
            hard_limit = subscription.get("hard_limit_usd")
            total_usage = usage.get("total_usage")
            if not isinstance(hard_limit, (int, float)) or not isinstance(total_usage, (int, float)):
                raise ValueError("billing fallback missing hard_limit_usd/total_usage")
            balance = float(hard_limit) - float(total_usage) / 100.0
            return BalanceProbe(
                account=account,
                adapter="newapi_billing_fallback",
                ok=True,
                balance=balance,
                url=usage_url,
            )
        except Exception as exc:
            return BalanceProbe(
                account=account,
                adapter="newapi_token",
                ok=False,
                url=urljoin(root, "api/usage/token"),
                error=f"{first_error}; fallback failed: {exc}",
            )

    async def _get_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        api_key: str = "",
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "User-Agent": "Mozilla/5.0 AstrBot-Sub2API-Balance-Monitor/0.2",
        }
        if api_key:
            request_headers["Authorization"] = f"Bearer {api_key}"
        if headers:
            request_headers.update(headers)
        async with session.get(url, headers=request_headers) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ValueError(f"HTTP {resp.status}: {text[:160]}")
            try:
                data = json.loads(text)
            except Exception as exc:
                raise ValueError(f"invalid json: {text[:120]}") from exc
            if not isinstance(data, dict):
                raise ValueError("json response is not an object")
            return data

    async def _post_json(
        self,
        session: aiohttp.ClientSession,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        request_headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 AstrBot-Sub2API-Balance-Monitor/0.2",
        }
        if headers:
            request_headers.update(headers)
        async with session.post(url, headers=request_headers, json=payload) as resp:
            text = await resp.text()
            if resp.status >= 400:
                raise ValueError(f"HTTP {resp.status}: {text[:160]}")
            try:
                data = json.loads(text)
            except Exception as exc:
                raise ValueError(f"invalid json: {text[:120]}") from exc
            if not isinstance(data, dict):
                raise ValueError("json response is not an object")
            return data

    def _unwrap_sub2api_response(self, payload: Any) -> Any:
        if not isinstance(payload, dict):
            raise ValueError("response is not an object")
        if "code" in payload and payload.get("code") != 0:
            raise ValueError(str(payload.get("message") or "code != 0"))
        return payload.get("data")

    def _parse_newapi_groups_payload(self, payload: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(payload, dict):
            return {}
        data = payload.get("data") or {}
        if not isinstance(data, dict):
            return {}

        normalized: dict[str, dict[str, Any]] = {}
        for name in sorted(data.keys()):
            info = data.get(name) or {}
            if not isinstance(info, dict):
                info = {}
            ratio_value, ratio_type = _normalize_group_ratio(info.get("ratio"))
            normalized[str(name)] = {
                "ratio": ratio_value,
                "ratio_type": ratio_type,
                "desc": info.get("desc", ""),
            }
        return normalized

    def _parse_sub2api_groups(self, groups_payload: Any, rates_payload: Any = None) -> dict[str, dict[str, Any]]:
        if isinstance(groups_payload, dict) and "data" in groups_payload:
            groups_payload = groups_payload.get("data")
        if isinstance(rates_payload, dict) and "data" in rates_payload:
            rates_payload = rates_payload.get("data")
        if not isinstance(groups_payload, list):
            return {}

        rates: dict[str, Any] = {}
        if isinstance(rates_payload, dict):
            rates = {str(key): value for key, value in rates_payload.items()}

        normalized: dict[str, dict[str, Any]] = {}
        for item in groups_payload:
            if not isinstance(item, dict):
                continue
            group_id = item.get("id")
            name = str(item.get("name") or group_id or "").strip()
            if not name:
                continue
            base_ratio = item.get("rate_multiplier")
            user_ratio = rates.get(str(group_id))
            ratio_value, ratio_type = _normalize_group_ratio(user_ratio if user_ratio is not None else base_ratio)
            normalized[name] = {
                "ratio": ratio_value,
                "ratio_type": ratio_type,
                "desc": item.get("description") or "",
                "id": group_id,
                "platform": item.get("platform") or "",
                "base_ratio": base_ratio,
                "user_ratio": user_ratio,
                "status": item.get("status") or "",
                "is_exclusive": bool(item.get("is_exclusive")),
                "subscription_type": item.get("subscription_type") or "",
                "rpm_limit": item.get("rpm_limit"),
            }
        return normalized

    def _groups_to_rates(self, groups: dict[str, dict[str, Any]]) -> dict[str, float]:
        rates: dict[str, float] = {}
        for name, item in groups.items():
            ratio = item.get("ratio") if isinstance(item, dict) else None
            if isinstance(ratio, (int, float)):
                rates[name] = float(ratio)
        return rates

    def _ratio_watch_config_for(self, section: str, upstream: str, root_url: str) -> dict[str, Any]:
        ratio_watch = self.config.get("ratio_watch") or {}
        section_config = ratio_watch.get(section) if isinstance(ratio_watch, dict) else {}
        if not isinstance(section_config, dict):
            return {}
        for key in (upstream, root_url):
            value = section_config.get(key)
            if isinstance(value, dict):
                return value
        return {}

    def _configured_site_type(self, upstream: str, root_url: str) -> str:
        ratio_watch = self.config.get("ratio_watch") or {}
        site_types = ratio_watch.get("site_types") if isinstance(ratio_watch, dict) else {}
        if isinstance(site_types, dict):
            value = site_types.get(upstream, site_types.get(root_url, ""))
            if str(value).lower() in {"newapi", "sub2api"}:
                return str(value).lower()
        return ""

    def _sub2api_headers(self, access_token: str) -> dict[str, str]:
        token = str(access_token or "").strip()
        if token.lower().startswith("bearer "):
            return {"Authorization": token}
        return {"Authorization": f"Bearer {token}"}

    def _credentials_from_notes(self, notes: str) -> tuple[str, str] | None:
        lines = [line.strip() for line in str(notes or "").splitlines() if line.strip()]
        if len(lines) < 2:
            return None
        username, password = lines[0], lines[1]
        if not username or not password:
            return None
        return username, password

    def _sub2api_note_auths(self, accounts: list[Account]) -> list[tuple[Account, dict[str, Any]]]:
        auths: list[tuple[Account, dict[str, Any]]] = []
        seen: set[tuple[str, str]] = set()
        for account in accounts:
            credentials = self._credentials_from_notes(account.notes)
            if not credentials:
                continue
            username, password = credentials
            key = (username, password)
            if key in seen:
                continue
            seen.add(key)
            auths.append(
                (
                    account,
                    {
                        "auth_mode": "password",
                        "username": username,
                        "password": password,
                    },
                )
            )
        return auths

    async def _fetch_sub2api_groups_by_token(
        self,
        session: aiohttp.ClientSession,
        root_url: str,
        access_token: str,
    ) -> tuple[dict[str, dict[str, Any]], str]:
        headers = self._sub2api_headers(access_token)
        groups_payload = await self._get_json(session, _sub2api_groups_available_url(root_url), headers=headers)
        groups_data = self._unwrap_sub2api_response(groups_payload)
        rates_data: Any = {}
        try:
            rates_payload = await self._get_json(session, _sub2api_groups_rates_url(root_url), headers=headers)
            parsed_rates = self._unwrap_sub2api_response(rates_payload)
            if isinstance(parsed_rates, dict):
                rates_data = parsed_rates
        except Exception:
            rates_data = {}
        return self._parse_sub2api_groups(groups_data, rates_data), _sub2api_groups_available_url(root_url)

    async def _fetch_sub2api_groups_with_auth(
        self,
        session: aiohttp.ClientSession,
        root_url: str,
        auth: dict[str, Any],
    ) -> tuple[dict[str, dict[str, Any]], str]:
        auth_mode = str(auth.get("auth_mode") or "password").strip().lower()
        if auth_mode == "token":
            access_token = str(auth.get("access_token") or "").strip()
            refresh_token = str(auth.get("refresh_token") or "").strip()
            if not access_token:
                raise ValueError("sub2api auth_mode=token requires access_token")
            try:
                return await self._fetch_sub2api_groups_by_token(session, root_url, access_token)
            except Exception:
                if not refresh_token:
                    raise
                refresh_payload = await self._post_json(
                    session,
                    _sub2api_refresh_url(root_url),
                    {"refresh_token": refresh_token},
                )
                refresh_data = self._unwrap_sub2api_response(refresh_payload)
                if not isinstance(refresh_data, dict) or not refresh_data.get("access_token"):
                    raise ValueError("refresh succeeded but access_token missing")
                return await self._fetch_sub2api_groups_by_token(
                    session,
                    root_url,
                    str(refresh_data.get("access_token") or ""),
                )

        username = str(auth.get("username") or auth.get("email") or "").strip()
        password = str(auth.get("password") or "")
        if not username or not password:
            raise ValueError("sub2api requires username/password or auth_token config")
        login_payload = await self._post_json(
            session,
            _sub2api_login_url(root_url),
            {"email": username, "password": password},
        )
        login_data = self._unwrap_sub2api_response(login_payload)
        if not isinstance(login_data, dict) or not login_data.get("access_token"):
            raise ValueError("login succeeded but access_token missing")
        return await self._fetch_sub2api_groups_by_token(
            session,
            root_url,
            str(login_data.get("access_token") or ""),
        )

    async def _fetch_newapi_groups(
        self,
        session: aiohttp.ClientSession,
        root_url: str,
        auth: dict[str, Any] | None = None,
    ) -> tuple[dict[str, dict[str, Any]], str]:
        auth = auth or {}
        access_token = str(auth.get("access_token") or "").strip()
        user_id = str(auth.get("user_id") or auth.get("access_user_id") or "").strip()
        headers: dict[str, str] = {}
        urls = [_newapi_user_groups_url(root_url)]
        if access_token:
            token = access_token.removeprefix("Bearer ").removeprefix("bearer ").strip()
            headers["Authorization"] = token
            if user_id:
                headers["New-Api-User"] = user_id
            urls = [_newapi_user_self_groups_url(root_url), _newapi_user_groups_url(root_url)]

        errors: list[str] = []
        for url in urls:
            try:
                payload = await self._get_json(session, url, headers=headers)
                if not payload.get("success"):
                    errors.append(f"{url}: success=false")
                    continue
                groups = self._parse_newapi_groups_payload(payload)
                if groups:
                    return groups, url
                errors.append(f"{url}: no groups")
            except Exception as exc:
                errors.append(f"{url}: {exc}")
        raise ValueError("; ".join(errors[-3:]))

    async def _looks_like_sub2api_site(
        self,
        session: aiohttp.ClientSession,
        accounts: list[Account],
    ) -> bool:
        for account in accounts[:3]:
            try:
                payload = await self._get_json(session, _usage_url(account.base_url), account.api_key)
                if isinstance(payload, dict) and (
                    "balance" in payload or "remaining" in payload or "daily_usage" in payload
                ):
                    return True
            except Exception:
                continue
        return False

    async def _probe_group_rates(
        self,
        session: aiohttp.ClientSession,
        upstream: str,
        root_url: str,
        accounts: list[Account],
    ) -> GroupRateReport:
        errors: list[str] = []
        site_type = self._configured_site_type(upstream, root_url)
        newapi_auth = self._ratio_watch_config_for("newapi_auth", upstream, root_url)
        sub2api_note_auths = self._sub2api_note_auths(accounts)

        async def probe_sub2api_with_notes() -> GroupRateReport:
            if not sub2api_note_auths:
                return GroupRateReport(
                    upstream=upstream,
                    root_url=root_url,
                    accounts=accounts,
                    ok=False,
                    skipped=True,
                    platform="sub2api",
                    url=_sub2api_groups_available_url(root_url),
                    error="skip sub2api ratio watch: no enabled account has username/password in notes",
                )
            sub2api_errors: list[str] = []
            for account, auth in sub2api_note_auths:
                try:
                    groups, url = await self._fetch_sub2api_groups_with_auth(session, root_url, auth)
                    return GroupRateReport(
                        upstream=upstream,
                        root_url=root_url,
                        accounts=accounts,
                        ok=True,
                        source=f"/api/v1/groups/available via notes:{account.name}",
                        platform="sub2api",
                        groups=groups,
                        rates=self._groups_to_rates(groups),
                        url=url,
                    )
                except Exception as exc:
                    sub2api_errors.append(f"sub2api notes auth {account.name}: {exc}")
            return GroupRateReport(
                upstream=upstream,
                root_url=root_url,
                accounts=accounts,
                ok=False,
                skipped=True,
                platform="sub2api",
                url=_sub2api_groups_available_url(root_url),
                error="skip sub2api ratio watch: " + "; ".join(sub2api_errors[-4:]),
            )

        if site_type == "sub2api":
            return await probe_sub2api_with_notes()

        if site_type == "" and await self._looks_like_sub2api_site(session, accounts):
            return await probe_sub2api_with_notes()

        if site_type in {"", "newapi"}:
            try:
                groups, url = await self._fetch_newapi_groups(session, root_url, newapi_auth)
                return GroupRateReport(
                    upstream=upstream,
                    root_url=root_url,
                    accounts=accounts,
                    ok=True,
                    source="/api/user/groups",
                    platform="newapi",
                    groups=groups,
                    rates=self._groups_to_rates(groups),
                    url=url,
                )
            except Exception as exc:
                errors.append(f"newapi /api/user/groups: {exc}")
                if "success=false" in str(exc):
                    return GroupRateReport(
                        upstream=upstream,
                        root_url=root_url,
                        accounts=accounts,
                        ok=False,
                        platform="newapi",
                        url=_newapi_user_groups_url(root_url),
                        error="; ".join(errors[-4:]),
                    )
                if site_type == "newapi":
                    return GroupRateReport(
                        upstream=upstream,
                        root_url=root_url,
                        accounts=accounts,
                        ok=False,
                        platform="newapi",
                        url=_newapi_user_groups_url(root_url),
                        error="; ".join(errors[-4:]),
                    )

        return GroupRateReport(
            upstream=upstream,
            root_url=root_url,
            accounts=accounts,
            ok=False,
            platform="unknown",
            url=_newapi_user_groups_url(root_url),
            error="; ".join(errors[-4:]),
        )

    async def check(self) -> list[ChannelReport]:
        accounts = await self.load_accounts()
        grouped = self.group_accounts(accounts)
        timeout = aiohttp.ClientTimeout(total=float(self.config["http_timeout_seconds"]))
        reports: list[ChannelReport] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for upstream, channel_accounts in grouped.items():
                report = ChannelReport(upstream=upstream, accounts=channel_accounts)
                seen: set[tuple[str, str, str]] = set()
                tasks = []
                for account in channel_accounts:
                    adapter = self.adapter_for(account.upstream) or "skipped"
                    key = (adapter, _normalize_base_url(account.base_url), account.api_key)
                    if key in seen:
                        continue
                    seen.add(key)
                    tasks.append(self.probe_account(session, account))
                if tasks:
                    report.probes = list(await asyncio.gather(*tasks))
                reports.append(report)
        return reports

    async def check_group_rates(self) -> list[GroupRateReport]:
        accounts = await self.load_accounts()
        grouped: dict[tuple[str, str], list[Account]] = {}
        for account in accounts:
            root_url = _display_root_url(account.base_url)
            if not root_url:
                continue
            grouped.setdefault((account.upstream, root_url), []).append(account)

        timeout = aiohttp.ClientTimeout(total=float(self.config["http_timeout_seconds"]))
        reports: list[GroupRateReport] = []
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = [
                self._probe_group_rates(session, upstream, root_url, channel_accounts)
                for (upstream, root_url), channel_accounts in grouped.items()
            ]
            if tasks:
                reports = list(await asyncio.gather(*tasks))
        return reports


@register(
    PLUGIN_NAME,
    "Codex",
    "Monitor sub2api upstream channel balances",
    "0.1.0",
)
class Sub2APIBalanceMonitor(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data_dir = Path(get_astrbot_data_path()) / "plugin_data" / PLUGIN_NAME
        self.config_path = self.data_dir / "config.json"
        self.state_path = self.data_dir / "state.json"
        self.config = self._load_config()
        self.service = Sub2APIBalanceService(self.config)
        self._task: asyncio.Task | None = None
        self._check_lock = asyncio.Lock()

    def _load_config(self) -> dict[str, Any]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        existing = _load_json(self.config_path, {})
        config = _deep_merge(DEFAULT_CONFIG, existing if isinstance(existing, dict) else {})
        env_db = {
            "host": os.getenv("SUB2API_DB_HOST"),
            "port": os.getenv("SUB2API_DB_PORT"),
            "user": os.getenv("SUB2API_DB_USER"),
            "password": os.getenv("SUB2API_DB_PASSWORD"),
            "database": os.getenv("SUB2API_DB_NAME"),
        }
        for key, value in env_db.items():
            if value:
                config["db"][key] = int(value) if key == "port" else value
        saved_config = json.loads(json.dumps(config))
        saved_config["db"]["password"] = ""
        _save_json(self.config_path, saved_config)
        return config

    def _load_state(self) -> dict[str, Any]:
        state = _load_json(self.state_path, {})
        if not isinstance(state, dict):
            return {}
        state.setdefault("sessions", [])
        state.setdefault("low_alerted", {})
        state.setdefault("pending_alerts", [])
        state.setdefault("rate_snapshots", {})
        return state

    def _save_state(self, state: dict[str, Any]) -> None:
        _save_json(self.state_path, state)

    async def initialize(self) -> None:
        if self.config.get("enabled", True):
            self._task = asyncio.create_task(self._monitor_loop())
            logger.info("[%s] monitor loop started", PLUGIN_NAME)

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    async def _monitor_loop(self) -> None:
        await asyncio.sleep(20)
        while True:
            try:
                await self._run_check(send_alerts=True)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("[%s] monitor check failed: %s", PLUGIN_NAME, exc)
            await asyncio.sleep(max(60, int(self.config.get("check_interval_seconds", 600))))

    async def _run_check(self, send_alerts: bool) -> list[ChannelReport]:
        async with self._check_lock:
            reports = await self.service.check()
            messages: list[str] = []
            if send_alerts:
                state = self._load_state()
                messages = self._build_alerts(reports, state)
                if self.config.get("rate_monitor_enabled", True):
                    rate_reports = await self.service.check_group_rates()
                    messages.extend(self._build_rate_alerts(rate_reports, state))
                self._save_state(state)
        if send_alerts and messages:
            await self._send_alerts(messages)
        return reports

    def _build_alerts(self, reports: list[ChannelReport], state: dict[str, Any]) -> list[str]:
        threshold = float(self.config.get("threshold", 2.0))
        recovered_threshold = float(self.config.get("recovered_threshold", threshold))
        repeat_seconds = max(60, int(self.config.get("alert_repeat_seconds", 7200)))
        low_alerted: dict[str, Any] = state.setdefault("low_alerted", {})
        messages: list[str] = []
        now = int(time.time())

        for report in reports:
            balance = report.balance
            if balance is None:
                continue
            if balance >= recovered_threshold and report.upstream in low_alerted:
                low_alerted.pop(report.upstream, None)
                continue
            if balance >= threshold:
                continue
            previous = low_alerted.get(report.upstream)
            if isinstance(previous, dict):
                last_alerted_at = int(previous.get("alerted_at") or 0)
                if now - last_alerted_at < repeat_seconds:
                    previous["balance"] = balance
                    previous["checked_at"] = now
                    continue
            low_alerted[report.upstream] = {
                "balance": balance,
                "alerted_at": now,
                "checked_at": now,
            }
            lines = [
                "中转站渠道余额告警",
                f"渠道：{report.upstream}",
                f"当前余额：{_fmt_money(balance)} USD",
                f"阈值：{_fmt_money(threshold)} USD",
                f"涉及账号：{', '.join(a.name for a in report.accounts)}",
            ]
            lines.extend(self._format_recharge_urls(self._recharge_urls_for(report)))
            messages.append("\n".join(lines))
        return messages

    def _rate_snapshot_key(self, report: GroupRateReport) -> str:
        return f"{report.upstream}|{report.root_url}"

    def _snapshot_groups_for_compare(self, raw: Any) -> dict[str, dict[str, Any]]:
        if isinstance(raw, dict) and isinstance(raw.get("groups"), dict):
            raw = raw.get("groups")
        if not isinstance(raw, dict):
            return {}
        groups: dict[str, dict[str, Any]] = {}
        for name, item in raw.items():
            if isinstance(item, dict):
                groups[str(name)] = item
            elif isinstance(item, (int, float, str)):
                ratio, ratio_type = _normalize_group_ratio(item)
                groups[str(name)] = {"ratio": ratio, "ratio_type": ratio_type, "desc": ""}
        return groups

    def _diff_groups(
        self,
        old_groups: dict[str, dict[str, Any]],
        new_groups: dict[str, dict[str, Any]],
    ) -> list[dict[str, Any]]:
        changes: list[dict[str, Any]] = []
        old_names = set(old_groups)
        new_names = set(new_groups)

        for name in sorted(new_names - old_names):
            changes.append({
                "change_type": "group_added",
                "group_name": name,
                "old_value": None,
                "new_value": new_groups[name],
                "change_percent": None,
            })
        for name in sorted(old_names - new_names):
            changes.append({
                "change_type": "group_removed",
                "group_name": name,
                "old_value": old_groups[name],
                "new_value": None,
                "change_percent": None,
            })
        for name in sorted(old_names & new_names):
            old_item = old_groups[name]
            new_item = new_groups[name]
            if old_item.get("ratio") != new_item.get("ratio"):
                old_ratio = old_item.get("ratio")
                new_ratio = new_item.get("ratio")
                change_percent = None
                if isinstance(old_ratio, (int, float)) and isinstance(new_ratio, (int, float)) and old_ratio != 0:
                    change_percent = round((float(new_ratio) - float(old_ratio)) / float(old_ratio) * 100, 2)
                changes.append({
                    "change_type": "ratio_changed",
                    "group_name": name,
                    "old_value": old_item,
                    "new_value": new_item,
                    "change_percent": change_percent,
                })
            if old_item.get("desc") != new_item.get("desc"):
                changes.append({
                    "change_type": "desc_changed",
                    "group_name": name,
                    "old_value": old_item.get("desc"),
                    "new_value": new_item.get("desc"),
                    "change_percent": None,
                })
            for field, label in (
                ("status", "状态"),
                ("is_exclusive", "专属分组"),
                ("subscription_type", "订阅类型"),
                ("rpm_limit", "RPM 限制"),
                ("platform", "平台"),
            ):
                if field in old_item or field in new_item:
                    if old_item.get(field) != new_item.get(field):
                        changes.append({
                            "change_type": f"{field}_changed",
                            "group_name": name,
                            "old_value": old_item.get(field),
                            "new_value": new_item.get(field),
                            "change_percent": None,
                            "label": label,
                        })
        return changes

    def _format_change_value(self, raw: Any) -> str:
        if raw is None:
            return "-"
        if isinstance(raw, dict) and "ratio" in raw:
            ratio = raw.get("ratio")
            try:
                return f"{float(ratio):.4f}".rstrip("0").rstrip(".") + "x"
            except Exception:
                return str(ratio)
        return str(raw)

    def _change_direction(self, change: dict[str, Any]) -> str:
        def ratio_number(raw: Any) -> float | None:
            if isinstance(raw, dict):
                raw = raw.get("ratio")
            try:
                return float(raw)
            except (TypeError, ValueError):
                return None

        old_ratio = ratio_number(change.get("old_value"))
        new_ratio = ratio_number(change.get("new_value"))
        if old_ratio is None or new_ratio is None:
            return "changed"
        if new_ratio > old_ratio:
            return "up"
        if new_ratio < old_ratio:
            return "down"
        return "changed"

    def _build_rate_alerts(self, reports: list[GroupRateReport], state: dict[str, Any]) -> list[str]:
        snapshots: dict[str, Any] = state.setdefault("rate_snapshots", {})
        messages: list[str] = []
        now = int(time.time())

        for report in reports:
            key = self._rate_snapshot_key(report)
            if not report.ok or (not report.groups and not report.rates):
                previous = snapshots.get(key)
                if isinstance(previous, dict):
                    previous["last_error"] = report.error
                    previous["checked_at"] = now
                continue

            current_groups = report.groups or {
                name: {"ratio": round(float(rate), 8), "ratio_type": "number", "desc": ""}
                for name, rate in sorted(report.rates.items())
                if isinstance(rate, (int, float))
            }
            previous = snapshots.get(key)
            snapshots[key] = {
                "upstream": report.upstream,
                "root_url": report.root_url,
                "source": report.source,
                "platform": report.platform,
                "url": report.url,
                "groups": current_groups,
                "rates": self.service._groups_to_rates(current_groups),
                "checked_at": now,
                "account_names": [account.name for account in report.accounts],
            }
            if not isinstance(previous, dict):
                continue

            previous_groups = self._snapshot_groups_for_compare(previous)
            if not previous_groups:
                continue
            changes = self._diff_groups(previous_groups, current_groups)

            if not changes:
                continue

            up_changes = [item for item in changes if item.get("change_type") == "ratio_changed" and self._change_direction(item) == "up"]
            down_changes = [item for item in changes if item.get("change_type") == "ratio_changed" and self._change_direction(item) == "down"]
            changed_ratio = [item for item in changes if item.get("change_type") == "ratio_changed" and self._change_direction(item) == "changed"]
            added = [item for item in changes if item.get("change_type") == "group_added"]
            removed = [item for item in changes if item.get("change_type") == "group_removed"]
            desc_changed = [item for item in changes if item.get("change_type") == "desc_changed"]
            other_changed = [
                item for item in changes
                if item.get("change_type") not in {"ratio_changed", "group_added", "group_removed", "desc_changed"}
            ]
            lines = [
                "上游分组倍率变更",
                f"上游：{report.upstream}",
                f"平台：{report.platform or 'unknown'}",
                f"站点：{report.root_url}",
                f"来源：{report.source}",
                f"接口：{report.url}",
                f"涉及账号：{', '.join(account.name for account in report.accounts)}",
                f"变化数量：{len(changes)}",
            ]

            def append_ratio_block(title: str, items: list[dict[str, Any]], suffix: str) -> None:
                if not items:
                    return
                lines.extend(["", title])
                for change in items[:8]:
                    percent = change.get("change_percent")
                    extra = ""
                    if isinstance(percent, (int, float)):
                        extra = f"，{suffix} {abs(percent):.2f}%".rstrip("0").rstrip(".")
                    elif suffix:
                        extra = f"，{suffix}"
                    lines.append(
                        f"- {change.get('group_name') or '-'}：{self._format_change_value(change.get('old_value'))} -> {self._format_change_value(change.get('new_value'))}{extra}"
                    )

            append_ratio_block("涨价了，钱包先别眨眼：", up_changes, "上涨")
            append_ratio_block("降价了，这波可以多看两眼：", down_changes, "下降")

            if changed_ratio:
                lines.extend(["", "倍率变了，但方向不太好判断："])
                for change in changed_ratio[:8]:
                    lines.append(
                        f"- {change.get('group_name') or '-'}：{self._format_change_value(change.get('old_value'))} -> {self._format_change_value(change.get('new_value'))}"
                    )
            if added:
                lines.extend(["", "新分组上线："])
                for change in added[:8]:
                    lines.append(f"- {change.get('group_name') or '-'}：{self._format_change_value(change.get('new_value'))}")
            if removed:
                lines.extend(["", "分组下线了："])
                for change in removed[:8]:
                    lines.append(f"- {change.get('group_name') or '-'}：原倍率 {self._format_change_value(change.get('old_value'))}")
            if desc_changed:
                lines.extend(["", "描述有变化："])
                for change in desc_changed[:8]:
                    lines.append(f"- {change.get('group_name') or '-'}")
            if other_changed:
                lines.extend(["", "其他配置变化："])
                for change in other_changed[:8]:
                    lines.append(
                        f"- {change.get('group_name') or '-'}：{self._format_change_value(change.get('old_value'))} -> {self._format_change_value(change.get('new_value'))}"
                    )
            messages.append("\n".join(lines))
        return messages

    def _recharge_urls_for(self, report: ChannelReport) -> list[str]:
        configured = self.config.get("recharge_urls") or {}
        if isinstance(configured, dict):
            override = configured.get(report.upstream)
            if isinstance(override, str) and override.strip():
                return [override.strip()]
            if isinstance(override, list):
                urls = [str(item).strip() for item in override if str(item).strip()]
                if urls:
                    return urls

        urls: list[str] = []
        seen: set[str] = set()
        for account in report.accounts:
            url = _display_root_url(account.base_url)
            if not url or url in seen:
                continue
            seen.add(url)
            urls.append(url)
        fallback = str(self.config.get("recharge_url") or DEFAULT_RECHARGE_URL).strip()
        if not urls and fallback:
            urls.append(fallback)
        return urls

    def _format_recharge_urls(self, urls: list[str]) -> list[str]:
        if not urls:
            return ["充值链接：未配置"]
        if len(urls) == 1:
            return [f"充值链接：{urls[0]}"]
        return ["充值链接：", *[f"- {url}" for url in urls]]

    async def _build_test_alert(self) -> str:
        threshold = float(self.config.get("threshold", 2.0))
        test_balance = max(0.01, threshold - 0.77)
        upstream = "test-channel"
        account_names = "test-channel-default"
        recharge_lines = ["充值链接：未配置"]
        try:
            accounts = await self.service.load_accounts()
            grouped = self.service.group_accounts(accounts)
            for candidate in sorted(grouped):
                if self.service.adapter_for(candidate) is None:
                    continue
                report = ChannelReport(upstream=candidate, accounts=grouped[candidate])
                upstream = candidate
                account_names = ", ".join(a.name for a in report.accounts)
                recharge_lines = self._format_recharge_urls(self._recharge_urls_for(report))
                break
        except Exception as exc:
            logger.warning("[%s] build test alert with real channel failed: %s", PLUGIN_NAME, exc)

        lines = [
            "中转站渠道余额告警（测试）",
            f"渠道：{upstream}",
            f"当前余额：{_fmt_money(test_balance)} USD",
            f"阈值：{_fmt_money(threshold)} USD",
            f"涉及账号：{account_names}",
        ]
        lines.extend(recharge_lines)
        return "\n".join(lines)

    async def _send_alerts(self, messages: list[str]) -> None:
        state = self._load_state()
        sessions = [s for s in state.get("sessions", []) if isinstance(s, str) and s]
        if not sessions:
            state.setdefault("pending_alerts", []).extend(messages)
            self._save_state(state)
            logger.warning("[%s] no bound sessions; alerts stored as pending", PLUGIN_NAME)
            return

        unsent: list[str] = []
        for message in messages:
            delivered = False
            for session in sessions:
                try:
                    ok = await self.context.send_message(session, MessageChain().message(message))
                    delivered = delivered or bool(ok)
                except Exception as exc:
                    logger.warning("[%s] send alert to %s failed: %s", PLUGIN_NAME, session, exc)
            if not delivered:
                unsent.append(message)
        if unsent:
            state.setdefault("pending_alerts", []).extend(unsent)
            self._save_state(state)

    async def _flush_pending_to_event(self, event: AstrMessageEvent) -> None:
        state = self._load_state()
        pending = state.get("pending_alerts") or []
        if not pending:
            return
        state["pending_alerts"] = []
        self._save_state(state)
        await event.send(MessageChain().message("\n\n".join(str(x) for x in pending)))

    def _format_reports(self, reports: list[ChannelReport], verbose: bool = False) -> str:
        lines = ["sub2api 渠道余额监控"]
        threshold = float(self.config.get("threshold", 2.0))
        lines.append(f"阈值：{_fmt_money(threshold)} USD")
        for report in sorted(reports, key=lambda r: r.upstream):
            balance = report.balance
            if report.skipped:
                status = "跳过"
            elif balance is None:
                status = "查询失败"
            elif balance < threshold:
                status = "低余额"
            else:
                status = "正常"
            lines.append(
                f"- {report.upstream}: {status}, 余额={_fmt_money(balance)} USD, 账号数={len(report.accounts)}"
            )
            if verbose:
                for probe in report.probes:
                    if probe.skipped:
                        detail = "skipped"
                    elif probe.ok:
                        detail = f"{probe.adapter} ok {_fmt_money(probe.balance)}"
                    else:
                        detail = f"{probe.adapter} failed {probe.error[:90]}"
                    lines.append(f"  #{probe.account.id} {probe.account.name}: {detail}")
                lines.extend(f"  充值链接: {url}" for url in self._recharge_urls_for(report))
        return "\n".join(lines)

    def _format_channels(self, reports: list[ChannelReport]) -> str:
        lines = ["识别到的启动调用渠道："]
        for report in sorted(reports, key=lambda r: r.upstream):
            account_names = ", ".join(a.name for a in report.accounts)
            adapter = self.service.adapter_for(report.upstream) or "skip"
            urls = ", ".join(self._recharge_urls_for(report)) or "未配置"
            lines.append(
                f"- {report.upstream} ({adapter}, {len(report.accounts)} 个账号): {account_names}; 链接: {urls}"
            )
        return "\n".join(lines)

    def _format_rate_reports(self, reports: list[GroupRateReport], verbose: bool = False) -> str:
        lines = ["上游分组倍率监控"]
        for report in sorted(reports, key=lambda r: (r.upstream, r.root_url)):
            if report.ok:
                lines.append(
                    f"- {report.upstream} {report.root_url}: 已读取 {len(report.groups or report.rates)} 个分组, 平台={report.platform}, 来源={report.source}"
                )
                if verbose:
                    groups = report.groups or {
                        name: {"ratio": rate} for name, rate in report.rates.items()
                    }
                    for name, item in sorted(groups.items()):
                        lines.append(f"  {name}: {self._format_change_value(item)}")
            elif report.skipped:
                lines.append(f"- {report.upstream} {report.root_url}: 已跳过倍率监控, 平台={report.platform or 'unknown'}")
                if verbose and report.error:
                    lines.append(f"  {report.error[:240]}")
            else:
                lines.append(f"- {report.upstream} {report.root_url}: 读取失败, 平台={report.platform or 'unknown'}")
                if verbose and report.error:
                    lines.append(f"  {report.error[:240]}")
        return "\n".join(lines)

    @filter.command("渠道监控", alias={"余额监控", "sub2api_balance"})
    async def balance_monitor(self, event: AstrMessageEvent, args: GreedyStr = ""):
        """管理 sub2api 上游渠道余额监控。"""
        await self._flush_pending_to_event(event)
        action = str(args or "").strip().lower()
        if action in {"绑定", "bind"}:
            state = self._load_state()
            sessions = state.setdefault("sessions", [])
            if event.unified_msg_origin not in sessions:
                sessions.append(event.unified_msg_origin)
                self._save_state(state)
            yield event.plain_result("已绑定当前会话为余额告警接收会话。")
            return

        if action in {"解绑", "unbind"}:
            state = self._load_state()
            state["sessions"] = [s for s in state.get("sessions", []) if s != event.unified_msg_origin]
            self._save_state(state)
            yield event.plain_result("已解绑当前会话。")
            return

        if action in {"测试", "test", "测试发送"}:
            state = self._load_state()
            sessions = state.setdefault("sessions", [])
            if event.unified_msg_origin not in sessions:
                sessions.append(event.unified_msg_origin)
                self._save_state(state)
            yield event.plain_result(await self._build_test_alert())
            return

        if action in {"列表", "list", "channels"}:
            reports = await self._run_check(send_alerts=False)
            yield event.plain_result(self._format_channels(reports))
            return

        if action in {"倍率", "rates", "rate"}:
            async with self._check_lock:
                reports = await self.service.check_group_rates()
            yield event.plain_result(self._format_rate_reports(reports, verbose=True))
            return

        if action in {"检查", "check", "now", "立即检查"}:
            reports = await self._run_check(send_alerts=True)
            yield event.plain_result(self._format_reports(reports, verbose=True))
            return

        if action in {"状态", "status", ""}:
            state = self._load_state()
            sessions = state.get("sessions", [])
            pending = len(state.get("pending_alerts") or [])
            lines = [
                "sub2api 渠道余额监控状态",
                f"启用：{bool(self.config.get('enabled', True))}",
                f"检查间隔：{int(self.config.get('check_interval_seconds', 600))} 秒",
                f"阈值：{_fmt_money(float(self.config.get('threshold', 2.0)))} USD",
                f"倍率监控：{bool(self.config.get('rate_monitor_enabled', True))}",
                f"告警会话数：{len(sessions)}",
                f"待补发告警：{pending}",
                "命令：渠道监控 绑定 / 检查 / 列表 / 倍率 / 测试 / 解绑",
            ]
            yield event.plain_result("\n".join(lines))
            return

        yield event.plain_result("未知参数。可用：绑定、检查、列表、倍率、状态、测试、解绑。")
