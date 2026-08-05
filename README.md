<div align="center">

<pre>
╭──────────────────────────────────────────────╮
│  ◈  B O T P L U G I N   M A S T E R  ◈       │
│      AstrBot extensions for daily ops        │
╰──────────────────────────────────────────────╯
</pre>

# BotPlugin Master

A focused collection of lightweight AstrBot plugins for research intelligence, platform operations, and local Agent operations.

![Runtime: AstrBot](https://img.shields.io/badge/Runtime-AstrBot-6D5EF7?style=flat-square)
![Python: 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![Plugins: 3](https://img.shields.io/badge/Plugins-3-F59E0B?style=flat-square)
![License: MIT](https://img.shields.io/badge/License-MIT-2EA043?style=flat-square)

</div>

**English** · [中文](README.zh-CN.md)

## Project Positioning

BotPlugin Master is a collection of AstrBot plugins for personal and small self-hosted deployments. Each plugin has its own directory, configuration, and documentation, with explicit input boundaries, readable alerts, and a strict rule against committing runtime secrets.

The repository currently covers three workflows:

- Research intelligence: discover new arXiv papers from a Zotero interest profile and generate a Chinese digest.
- Platform operations: monitor sub2api channel balances, multipliers, and operational changes.
- Agent operations: watch local Codex CLI rollout events and notify operators about completion or unexpected termination.

## Plugin Matrix

| Domain | Plugin | Scope | Primary dependencies | Documentation |
| --- | --- | --- | --- | --- |
| ![Research](https://img.shields.io/badge/RESEARCH-6D5EF7?style=flat-square) | zotero_arxiv_digest | Zotero profiling, arXiv retrieval, relevance scoring, and LLM digests | Zotero API, arXiv API, AstrBot LLM | [English](plugins/zotero_arxiv_digest/README.md) · [中文](plugins/zotero_arxiv_digest/README.zh-CN.md) |
| ![Operations](https://img.shields.io/badge/OPERATIONS-0284C7?style=flat-square) | sub2api_balance_monitor | Upstream balances, low-balance alerts, group multiplier changes, and session binding | PostgreSQL, upstream HTTP APIs | [English](plugins/sub2api_balance_monitor/README.md) · [中文](plugins/sub2api_balance_monitor/README.zh-CN.md) |
| ![Agent Ops](https://img.shields.io/badge/AGENT_OPS-F97316?style=flat-square) | astrbot_plugin_codex_monitor | Rollout events, completion notifications, unexpected-stop diagnostics, and status queries | Local Codex CLI sessions, AstrBot messaging platform | [English](plugins/astrbot_plugin_codex_monitor/README.md) · [中文](plugins/astrbot_plugin_codex_monitor/README.zh-CN.md) |

## Quick Start

### 1. Clone the repository

```bash
git clone git@github.com:GZWSAMA/BotPlugin_Master.git
cd BotPlugin_Master
```

### 2. Install the required plugins

Copy the plugin directories you need into the AstrBot plugin directory:

```bash
cp -r plugins/zotero_arxiv_digest <astrbot-root>/data/plugins/
cp -r plugins/sub2api_balance_monitor <astrbot-root>/data/plugins/
cp -r plugins/astrbot_plugin_codex_monitor <astrbot-root>/data/plugins/
```

When installing only one plugin, copy only its corresponding line. Install additional dependencies inside the AstrBot environment:

```bash
python -m pip install -r <astrbot-root>/data/plugins/zotero_arxiv_digest/requirements.txt
python -m pip install -r <astrbot-root>/data/plugins/sub2api_balance_monitor/requirements.txt
```

The Codex task monitor has no additional Python dependencies.

### 3. Configure and restart AstrBot

Each plugin includes a sanitized config.example.json. For plugins that use plugin_data configuration files, copy the template first:

```bash
mkdir -p <astrbot-root>/data/plugin_data/<plugin-name>
cp plugins/<plugin-name>/config.example.json <astrbot-root>/data/plugin_data/<plugin-name>/config.json
```

Plugins that provide _conf_schema.json use AstrBot-generated configuration. Fill it in through the WebUI or the generated data/config/<plugin-name>_config.json, then restart AstrBot. Never copy real runtime configuration back into this repository.

## Configuration and Runtime Data

| Plugin | Configuration | Runtime data |
| --- | --- | --- |
| zotero_arxiv_digest | data/plugin_data/zotero_arxiv_digest/config.json | Digest state and interest-profile cache |
| sub2api_balance_monitor | data/plugin_data/sub2api_balance_monitor/config.json | Alert, multiplier, and probe snapshots |
| astrbot_plugin_codex_monitor | data/config/astrbot_plugin_codex_monitor_config.json | data/plugin_data/astrbot_plugin_codex_monitor/monitor_state.json |

Configuration templates describe structure only. They do not contain real API keys, tokens, passwords, session IDs, or QQ OpenIDs. The Codex monitor may also read rollout logs containing prompts, tool arguments, and final replies; configure its target session explicitly and only send data to a session you control.

## Release Standards

Every plugin directory should contain at least:

- metadata.yaml: plugin name, version, author, and a professional description.
- README.md and README.zh-CN.md: installation, configuration, runtime boundaries, and security notes.
- config.example.json: a sanitized, copyable configuration template.
- requirements.txt: only when extra dependencies are required.

Do not commit runtime configuration, state files, logs, caches, credentials, or platform session identifiers. Before committing, run:

```bash
rg -n --hidden -S "(api[_-]?key|token|secret|password|Authorization|Bearer|sk-[A-Za-z0-9])" .
```

## Repository Layout

```text
BotPlugin_Master/
├── plugins/
│   ├── astrbot_plugin_codex_monitor/
│   ├── sub2api_balance_monitor/
│   └── zotero_arxiv_digest/
├── .gitignore
├── LICENSE
├── README.md
└── README.zh-CN.md
```

## Security Boundaries

- Prefer environment variables or AstrBot runtime configuration for data-source credentials.
- Each plugin should access only the data sources declared in its documentation and must not write real runtime state into the repository.
- External API, database, and messaging failures must remain explicit; empty data must never be used to disguise a failed run.
- Every new plugin must update its own README files, metadata.yaml, and configuration template together.

## License

MIT License. See [LICENSE](LICENSE).
