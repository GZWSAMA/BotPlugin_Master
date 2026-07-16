# BotPlugin Master

这个仓库收集了一组自用 AstrBot 插件，当前包含：

| 插件 | 功能 | 目录 |
| --- | --- | --- |
| Zotero arXiv 日报 | 根据 Zotero 文献库画像匹配 arXiv 新论文，并用 AstrBot 配置的 LLM 生成中文论文日报 | `plugins/zotero_arxiv_digest` |
| sub2api 渠道余额与倍率监控 | 从 sub2api 数据库读取启用账号，监控上游余额、低余额告警和 New API/sub2api 分组倍率变化 | `plugins/sub2api_balance_monitor` |

## 安装方式

任选一种方式安装插件：

1. 克隆整个仓库后复制需要的插件目录到 AstrBot 插件目录：

```bash
git clone git@github.com:GZWSAMA/BotPlugin_Master.git
cp -r BotPlugin_Master/plugins/zotero_arxiv_digest /opt/AstrBot/data/plugins/
cp -r BotPlugin_Master/plugins/sub2api_balance_monitor /opt/AstrBot/data/plugins/
```

2. 只下载单个插件目录，然后放到：

```text
/opt/AstrBot/data/plugins/<插件目录名>
```

安装后重启 AstrBot。首次启动插件会在 AstrBot 数据目录下自动生成运行配置：

```text
/opt/AstrBot/data/plugin_data/<插件名>/config.json
```

## 配置教程

- Zotero arXiv 日报：见 `plugins/zotero_arxiv_digest/README.md`
- sub2api 渠道余额与倍率监控：见 `plugins/sub2api_balance_monitor/README.md`

每个插件目录都提供了 `config.example.json`。请把示例内容按需复制到 AstrBot 自动生成的 `plugin_data/<插件名>/config.json` 中修改，不要把真实 `config.json` 提交到 Git。

## 安全说明

仓库只包含插件源码、依赖声明、文档和脱敏示例配置，不包含：

- Zotero API Key
- sub2api 数据库密码
- New API/sub2api 登录 token
- AstrBot 会话 ID
- 插件运行状态、历史告警或倍率快照

`.gitignore` 已排除常见运行配置、状态文件、环境变量文件和编译缓存。发布前仍建议执行一次敏感信息检查：

```bash
rg -n --hidden -S "(api[_-]?key|token|secret|password|Authorization|Bearer|sk-[A-Za-z0-9])" .
```

## License

MIT License. See `LICENSE`.
