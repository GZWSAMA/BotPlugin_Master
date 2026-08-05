# sub2api Balance and Multiplier Monitor

![Operations](https://img.shields.io/badge/OPERATIONS-0284C7?style=flat-square)
![Source: PostgreSQL](https://img.shields.io/badge/Source-PostgreSQL-0F766E?style=flat-square)
![Alerts: AstrBot](https://img.shields.io/badge/Alerts-AstrBot-16A34A?style=flat-square)

**English** · [中文](README.zh-CN.md)

> Operational observability for self-hosted channels: turn balance thresholds and multiplier changes into actionable AstrBot alerts.

The plugin reads enabled accounts from the sub2api PostgreSQL database, identifies upstream channels from account names, checks balances on a schedule, and sends low-balance alerts. It can also read New API/sub2api group multipliers and notify when a multiplier, group, or description changes.

## Capabilities

- Reads `accounts`, account groups, and account remarks from the database.
- Identifies an upstream channel from the account name format `<upstream>-<group>`.
- Supports the sub2api `/v1/usage` balance endpoint.
- Supports New API token usage and the OpenAI billing fallback.
- Can skip selected upstreams or designate upstreams that use New API token balance logic.
- Supports low-balance thresholds, recovery thresholds, and alert-repeat intervals.
- Monitors New API/sub2api group multiplier changes.
- Binds multiple AstrBot sessions as alert recipients.

## Installation

Copy this directory into the AstrBot plugin directory:

```bash
cp -r plugins/sub2api_balance_monitor /opt/AstrBot/data/plugins/
```

Install the dependencies in the AstrBot environment:

```bash
python -m pip install -r /opt/AstrBot/data/plugins/sub2api_balance_monitor/requirements.txt
```

When AstrBot runs in a container, install the dependencies inside that container or add them to the image or startup script.

After AstrBot restarts, the plugin creates:

```text
/opt/AstrBot/data/plugin_data/sub2api_balance_monitor/config.json
```

## Database Connection

Prefer environment variables for the database password so it is not written to `config.json`:

```bash
export SUB2API_DB_HOST=postgres
export SUB2API_DB_PORT=5432
export SUB2API_DB_USER=sub2api
export SUB2API_DB_PASSWORD='YOUR_DB_PASSWORD'
export SUB2API_DB_NAME=sub2api
```

The plugin reads these environment variables first. To limit accidental disclosure, it writes `db.password` back as an empty string when saving configuration.

When the runtime environment is controlled, `db.password` can also be set in AstrBot runtime configuration. Never commit it to Git.

## Account Naming Convention

The plugin monitors only enabled accounts whose names follow this format:

```text
<upstream>-<group>
```

Examples:

```text
openai-default
openai-premium
claude-main
```

The segment before `-` is treated as the upstream name. Low-balance alerts use the minimum account balance for that upstream.

## Configuration

Use `config.example.json` as a reference and edit:

```text
/opt/AstrBot/data/plugin_data/sub2api_balance_monitor/config.json
```

| Field | Description |
| --- | --- |
| `enabled` | Enables scheduled monitoring. |
| `check_interval_seconds` | Check interval; the effective minimum is 60 seconds. |
| `alert_repeat_seconds` | Repeat interval for a low-balance alert from the same upstream. |
| `threshold` | Low-balance threshold in USD. |
| `recovered_threshold` | Balance that clears the low-balance state. |
| `recharge_url` | Default top-up URL. |
| `recharge_urls` | Per-upstream top-up URL overrides. |
| `db` | sub2api PostgreSQL connection settings. |
| `newapi_token_upstreams` | Upstream names that use New API token balance logic. |
| `skip_upstreams` | Upstream names excluded from balance probing. |
| `rate_monitor_enabled` | Enables group multiplier monitoring. |
| `ratio_watch.site_types` | Explicit upstream or site type: `newapi` or `sub2api`. |
| `ratio_watch.newapi_auth` | Authentication required by New API multiplier endpoints. |
| `ratio_watch.sub2api_auth` | Authentication required by sub2api multiplier endpoints. |

## Multiplier Monitoring Authentication

### New API

No authentication is required when the public `/api/user/groups` endpoint is accessible. If the site requires a login session, configure it by upstream name or root URL:

```json
{
  "ratio_watch": {
    "site_types": {
      "openai": "newapi"
    },
    "newapi_auth": {
      "openai": {
        "access_token": "",
        "user_id": "OPTIONAL_USER_ID"
      }
    }
  }
}
```

### sub2api

The plugin first attempts to read login credentials from account remarks: the first line is the username or email and the second line is the password. You can also declare a site type in configuration to avoid a misclassification:

```json
{
  "ratio_watch": {
    "site_types": {
      "openai": "sub2api"
    }
  }
}
```

If you maintain tokens or account credentials in configuration, keep them only in AstrBot runtime configuration and never commit them to Git.

## Commands

```text
渠道监控 状态
渠道监控 绑定
渠道监控 解绑
渠道监控 检查
渠道监控 列表
渠道监控 倍率
渠道监控 测试
```

Aliases:

```text
余额监控
sub2api_balance
```

## Troubleshooting

- No alert recipient: send `渠道监控 绑定` in the target group or direct message.
- Database connection failed: check the `SUB2API_DB_*` environment variables, network access, and database permissions.
- An account is absent from the list: confirm that it is enabled, not deleted, schedulable, and named with `-`.
- An upstream does not support a balance endpoint: add it to `skip_upstreams` or, when appropriate, `newapi_token_upstreams`.
- Multiplier monitoring is skipped: the site usually requires login credentials or an authenticated endpoint.
