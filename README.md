<div align="center">

<pre>
╭──────────────────────────────────────────────╮
│  ◈  B O T P L U G I N   M A S T E R  ◈       │
│      AstrBot extensions for daily ops        │
╰──────────────────────────────────────────────╯
</pre>

# BotPlugin Master

面向自托管 AstrBot 的轻量插件集合，覆盖研究情报、平台可观测性和本地 Agent 运维。

![Runtime: AstrBot](https://img.shields.io/badge/Runtime-AstrBot-6D5EF7?style=flat-square)
![Python: 3.12](https://img.shields.io/badge/Python-3.12-3776AB?style=flat-square&logo=python&logoColor=white)
![Plugins: 3](https://img.shields.io/badge/Plugins-3-F59E0B?style=flat-square)
![License: MIT](https://img.shields.io/badge/License-MIT-2EA043?style=flat-square)

</div>

## 项目定位

BotPlugin Master 是一组面向个人和小型自托管部署的 AstrBot 插件。每个插件保持独立目录、独立配置和独立文档，强调清晰的输入边界、可读的告警内容和不提交运行时秘密。

项目当前聚焦三类工作流：

- 研究情报：从 Zotero 兴趣画像中筛选 arXiv 新论文并生成中文日报。
- 平台运营：监控 sub2api 渠道余额、倍率和异常变化。
- Agent 运维：监听本机 Codex CLI rollout，及时通知任务完成或异常终止。

## 插件矩阵

| 领域 | 插件 | 能力边界 | 主要依赖 | 文档 |
| --- | --- | --- | --- | --- |
| ![Research](https://img.shields.io/badge/RESEARCH-6D5EF7?style=flat-square) | `zotero_arxiv_digest` | Zotero 画像、arXiv 检索、相关性筛选和 LLM 日报 | Zotero API、arXiv API、AstrBot LLM | [插件说明](plugins/zotero_arxiv_digest/README.md) |
| ![Operations](https://img.shields.io/badge/OPERATIONS-0284C7?style=flat-square) | `sub2api_balance_monitor` | 上游余额、低余额告警、分组倍率变化和会话绑定 | PostgreSQL、上游 HTTP API | [插件说明](plugins/sub2api_balance_monitor/README.md) |
| ![Agent Ops](https://img.shields.io/badge/AGENT_OPS-F97316?style=flat-square) | `astrbot_plugin_codex_monitor` | rollout 事件、完成通知、异常中断诊断和状态查询 | 本机 Codex CLI sessions、AstrBot 消息平台 | [插件说明](plugins/astrbot_plugin_codex_monitor/README.md) |

## 快速开始

### 1. 获取仓库

```bash
git clone git@github.com:GZWSAMA/BotPlugin_Master.git
cd BotPlugin_Master
```

### 2. 安装需要的插件

把目标插件目录复制到 AstrBot 的插件目录：

```bash
cp -r plugins/zotero_arxiv_digest <astrbot-root>/data/plugins/
cp -r plugins/sub2api_balance_monitor <astrbot-root>/data/plugins/
cp -r plugins/astrbot_plugin_codex_monitor <astrbot-root>/data/plugins/
```

只安装一个插件时，复制对应的一行即可。需要额外依赖的插件在 AstrBot 环境中安装：

```bash
python -m pip install -r <astrbot-root>/data/plugins/zotero_arxiv_digest/requirements.txt
python -m pip install -r <astrbot-root>/data/plugins/sub2api_balance_monitor/requirements.txt
```

Codex 任务监控插件没有额外 Python 依赖。

### 3. 配置并重启 AstrBot

每个插件目录都提供脱敏的 `config.example.json`。对使用 `plugin_data` 配置文件的插件，可先复制模板：

```bash
mkdir -p <astrbot-root>/data/plugin_data/<plugin-name>
cp plugins/<plugin-name>/config.example.json <astrbot-root>/data/plugin_data/<plugin-name>/config.json
```

带有 `_conf_schema.json` 的插件由 AstrBot 生成配置文件，应在 WebUI 或生成的 `data/config/<plugin-name>_config.json` 中填写配置。最后重启 AstrBot，并且不要把真实配置覆盖回仓库。

## 配置与运行数据

| 插件 | 配置位置 | 运行数据 |
| --- | --- | --- |
| `zotero_arxiv_digest` | `data/plugin_data/zotero_arxiv_digest/config.json` | 日报状态和兴趣画像缓存 |
| `sub2api_balance_monitor` | `data/plugin_data/sub2api_balance_monitor/config.json` | 告警、倍率和探测快照 |
| `astrbot_plugin_codex_monitor` | `data/config/astrbot_plugin_codex_monitor_config.json` | `data/plugin_data/astrbot_plugin_codex_monitor/monitor_state.json` |

配置模板只描述结构，不包含真实 API Key、token、密码、会话 ID 或 QQ OpenID。Codex 监控插件还可能读取包含提示词、工具参数和最终回复的 rollout 日志，目标会话必须由部署者显式配置。

## 发布规范

每个插件目录至少应包含：

- `metadata.yaml`：插件名称、版本、作者和专业描述。
- `README.md`：安装、配置、运行边界和安全说明。
- `config.example.json`：可复制的脱敏配置模板。
- `requirements.txt`：只有存在额外依赖时才提供。

禁止提交运行配置、状态文件、日志、缓存、凭据和平台会话标识。提交前建议执行：

```bash
rg -n --hidden -S "(api[_-]?key|token|secret|password|Authorization|Bearer|sk-[A-Za-z0-9])" .
```

## 目录结构

```text
BotPlugin_Master/
├── plugins/
│   ├── astrbot_plugin_codex_monitor/
│   ├── sub2api_balance_monitor/
│   └── zotero_arxiv_digest/
├── .gitignore
├── LICENSE
└── README.md
```

## 安全边界

- 数据源凭据优先通过环境变量或 AstrBot 运行配置提供。
- 插件只访问其文档中声明的数据源，不把真实运行状态写入仓库。
- 外部 API、数据库和消息平台失败时，应保留明确的错误信息，不用空数据伪装成功。
- 任何新插件都应同时更新自己的 README、`metadata.yaml` 和配置模板。

## License

MIT License. See [LICENSE](LICENSE).
