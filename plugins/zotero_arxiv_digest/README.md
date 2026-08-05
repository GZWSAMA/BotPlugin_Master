# Zotero arXiv Digest

![Research](https://img.shields.io/badge/RESEARCH-6D5EF7?style=flat-square)
![Sources: Zotero + arXiv](https://img.shields.io/badge/Sources-Zotero_%2B_arXiv-B91C1C?style=flat-square)
![Output: Chinese Digest](https://img.shields.io/badge/Output-Chinese_Digest-2563EB?style=flat-square)

**English** · [中文](README.zh-CN.md)

> Turn a personal Zotero research trail into a readable daily arXiv discovery stream, with configurable selection boundaries and less irrelevant retrieval.

The plugin builds an interest profile from paper titles, abstracts, and tags in a Zotero library. It retrieves papers newly published in selected arXiv categories for a target date, filters for relevance, and asks the current AstrBot LLM to produce a Chinese daily digest.

## Capabilities

- Supports Zotero user libraries and group libraries.
- Can limit profiling to selected Zotero collections and automatically expands child collections.
- Supports custom arXiv categories, extra query conditions, maximum candidate counts, and lookback windows.
- Supports scheduled delivery and manual triggering.
- Falls back to a rule-based summary when no LLM provider is configured.

## Installation

Copy this directory into the AstrBot plugin directory:

```bash
cp -r plugins/zotero_arxiv_digest /opt/AstrBot/data/plugins/
```

After AstrBot restarts, the plugin creates:

```text
/opt/AstrBot/data/plugin_data/zotero_arxiv_digest/config.json
```

## Zotero Setup

1. Sign in to the Zotero web application and create a key from the API Keys page.
2. Grant the key at least read access to the target library.
3. For a user library, obtain the Zotero user ID.
4. For a group library, obtain the group ID and set `library_type` to `group`.
5. To build a profile from specific collections only, provide their collection keys. Leave the list empty to read recent items from the entire library.

## Configuration

Use `config.example.json` as a reference and edit:

```text
/opt/AstrBot/data/plugin_data/zotero_arxiv_digest/config.json
```

| Field | Description |
| --- | --- |
| `enabled` | Enables the scheduled job. |
| `timezone` | Time zone used by the scheduler. |
| `schedule_time` | Daily delivery time in `HH:MM` format. |
| `zotero.library_type` | `user` or `group`. |
| `zotero.user_id` | User ID for a user library. |
| `zotero.group_id` | Group ID for a group library. |
| `zotero.api_key` | Zotero API key. Never commit it to Git. |
| `zotero.collection_keys` | Collection-key allowlist. Leave empty to read the full library. |
| `arxiv.categories` | arXiv category list. |
| `arxiv.query_extra` | Additional arXiv query expression. |
| `matching.max_papers` | Maximum number of papers delivered in one digest. |
| `matching.min_score` | Minimum relevance score. |
| `llm.provider_id` | AstrBot LLM provider ID. Leave empty to use the current provider. |
| `send.sessions` | AstrBot session IDs that receive digests. Sessions can also be bound by command. |

## Commands

```text
论文推送 状态
论文推送 绑定
论文推送 检查
论文推送 立即
论文推送 测试
```

Aliases:

```text
arxiv日报
zotero_arxiv
```

## Troubleshooting

- Missing `zotero.api_key`: set the Zotero API key in the plugin runtime configuration.
- Missing `zotero.user_id`: this is required when `library_type=user`.
- Missing `zotero.group_id`: this is required when `library_type=group`.
- No scheduled delivery: send `论文推送 绑定` in the target group or direct message, or set `send.sessions` manually.
- LLM summarization failed: check the AstrBot default LLM provider or specify one with `llm.provider_id`.
