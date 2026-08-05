# Zotero arXiv 日报插件

![Research](https://img.shields.io/badge/RESEARCH-6D5EF7?style=flat-square)
![Sources: Zotero + arXiv](https://img.shields.io/badge/Sources-Zotero_%2B_arXiv-B91C1C?style=flat-square)
![Output: Chinese Digest](https://img.shields.io/badge/Output-Chinese_Digest-2563EB?style=flat-square)

> 把个人 Zotero 研究脉络变成每日可读的 arXiv 发现流，减少无关检索，保留可配置的筛选边界。

根据 Zotero 文献库中的论文标题、摘要和标签构建兴趣画像，查询指定 arXiv 分类在目标日期的新论文，筛选相关论文后调用 AstrBot 当前 LLM 生成中文日报。

## 功能

- 支持 Zotero user library 和 group library
- 可限定 Zotero collection，并自动展开子 collection
- 支持自定义 arXiv 分类、附加查询条件、最大候选数量和回看天数
- 支持定时推送和手动触发
- 未配置 LLM provider 时会回退到规则摘要

## 安装

把本目录复制到 AstrBot 插件目录：

```bash
cp -r plugins/zotero_arxiv_digest /opt/AstrBot/data/plugins/
```

重启 AstrBot 后，插件会生成：

```text
/opt/AstrBot/data/plugin_data/zotero_arxiv_digest/config.json
```

## Zotero 准备

1. 登录 Zotero 网页端，进入 API Keys 页面创建 key。
2. 权限至少需要读取目标文献库。
3. 如果使用 user library，准备 Zotero user id。
4. 如果使用 group library，准备 group id，并把 `library_type` 设置为 `group`。
5. 如果只想根据某几个 collection 建模，填入 collection key；留空则读取整个库的最近条目。

## 配置

参考 `config.example.json`，修改 AstrBot 运行目录下的：

```text
/opt/AstrBot/data/plugin_data/zotero_arxiv_digest/config.json
```

关键字段：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用定时任务 |
| `timezone` | 定时任务时区 |
| `schedule_time` | 每天推送时间，格式 `HH:MM` |
| `zotero.library_type` | `user` 或 `group` |
| `zotero.user_id` | user library 的用户 ID |
| `zotero.group_id` | group library 的 group ID |
| `zotero.api_key` | Zotero API Key，不要提交到 Git |
| `zotero.collection_keys` | 限定 collection key 列表，留空读取全库 |
| `arxiv.categories` | arXiv 分类列表 |
| `arxiv.query_extra` | 额外 arXiv 查询语句 |
| `matching.max_papers` | 最多推送论文数 |
| `matching.min_score` | 相关性最低分 |
| `llm.provider_id` | 指定 AstrBot LLM provider id；留空使用当前 provider |
| `send.sessions` | 接收推送的 AstrBot 会话 ID；也可用命令绑定 |

## 命令

```text
论文推送 状态
论文推送 绑定
论文推送 检查
论文推送 立即
论文推送 测试
```

别名：

```text
arxiv日报
zotero_arxiv
```

## 常见问题

- 提示缺少 `zotero.api_key`：在插件运行配置中填入 Zotero API Key。
- 提示缺少 `zotero.user_id`：`library_type=user` 时必须填写 user id。
- 提示缺少 `zotero.group_id`：`library_type=group` 时必须填写 group id。
- 没有自动推送：先在目标群/私聊发送 `论文推送 绑定`，或手动填写 `send.sessions`。
- LLM 摘要失败：检查 AstrBot 默认 LLM provider，或在 `llm.provider_id` 中指定 provider。
