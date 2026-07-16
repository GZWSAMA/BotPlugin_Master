# sub2api 渠道余额与倍率监控插件

从 sub2api PostgreSQL 数据库读取启用账号，按账号命名规则识别上游渠道，定时检查余额并发送低余额告警。同时支持读取 New API/sub2api 分组倍率，发现倍率、分组或描述变化时推送提醒。

## 功能

- 从数据库读取 `accounts`、账号分组和账号备注
- 按账号名 `<上游>-<分组>` 识别上游渠道
- 支持 sub2api `/v1/usage` 余额接口
- 支持 New API token 用量接口和 OpenAI billing fallback
- 支持跳过指定上游、指定 New API token 上游
- 支持余额低于阈值告警、恢复阈值和重复告警间隔
- 支持 New API/sub2api 分组倍率变化监控
- 支持绑定多个 AstrBot 会话接收告警

## 安装

把本目录复制到 AstrBot 插件目录：

```bash
cp -r plugins/sub2api_balance_monitor /opt/AstrBot/data/plugins/
```

安装依赖：

```bash
pip install -r /opt/AstrBot/data/plugins/sub2api_balance_monitor/requirements.txt
```

如果 AstrBot 运行在容器中，请在容器内安装依赖，或把依赖写入镜像/启动脚本。

重启 AstrBot 后，插件会生成：

```text
/opt/AstrBot/data/plugin_data/sub2api_balance_monitor/config.json
```

## 数据库连接

推荐用环境变量提供数据库密码，避免把密码写入 `config.json`：

```bash
export SUB2API_DB_HOST=postgres
export SUB2API_DB_PORT=5432
export SUB2API_DB_USER=sub2api
export SUB2API_DB_PASSWORD='YOUR_DB_PASSWORD'
export SUB2API_DB_NAME=sub2api
```

插件启动时会优先读取上述环境变量。为了避免泄露，插件保存配置时会把 `db.password` 写回为空字符串。

如果你确认运行环境安全，也可以在 AstrBot 运行配置里填写 `db.password`，但不要提交到 Git。

## 账号命名约定

插件只监控满足以下格式的启用账号：

```text
<上游>-<分组>
```

示例：

```text
openai-default
openai-premium
claude-main
```

`-` 前面的部分会被当作上游名称，低余额告警以同一上游下账号余额的最小值为准。

## 配置

参考 `config.example.json`，修改 AstrBot 运行目录下的：

```text
/opt/AstrBot/data/plugin_data/sub2api_balance_monitor/config.json
```

关键字段：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否启用定时监控 |
| `check_interval_seconds` | 检查间隔，最低实际间隔 60 秒 |
| `alert_repeat_seconds` | 同一上游重复低余额告警间隔 |
| `threshold` | 低余额阈值，单位 USD |
| `recovered_threshold` | 恢复阈值，余额达到该值后清除低余额状态 |
| `recharge_url` | 默认充值链接 |
| `recharge_urls` | 按上游覆盖充值链接 |
| `db` | sub2api PostgreSQL 连接信息 |
| `newapi_token_upstreams` | 使用 New API token 余额逻辑的上游名 |
| `skip_upstreams` | 跳过余额探测的上游名 |
| `rate_monitor_enabled` | 是否启用分组倍率监控 |
| `ratio_watch.site_types` | 指定上游或站点类型：`newapi`/`sub2api` |
| `ratio_watch.newapi_auth` | New API 倍率接口所需认证 |
| `ratio_watch.sub2api_auth` | sub2api 倍率接口所需认证 |

## 倍率监控认证

### New API

公开 `/api/user/groups` 可访问时无需配置认证。如果站点要求登录态，可按上游名或根 URL 配置：

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

插件会优先尝试从账号备注读取登录凭据：备注第一行写用户名/邮箱，第二行写密码。也可以在配置中指定站点类型，避免误判：

```json
{
  "ratio_watch": {
    "site_types": {
      "openai": "sub2api"
    }
  }
}
```

如果你选择在配置里维护 token 或账号密码，请只写在 AstrBot 运行配置中，不要提交到 Git。

## 命令

```text
渠道监控 状态
渠道监控 绑定
渠道监控 解绑
渠道监控 检查
渠道监控 列表
渠道监控 倍率
渠道监控 测试
```

别名：

```text
余额监控
sub2api_balance
```

## 常见问题

- 没有告警接收方：在目标群/私聊发送 `渠道监控 绑定`。
- 数据库连接失败：检查 `SUB2API_DB_*` 环境变量、网络和数据库权限。
- 账号没有出现在列表里：确认账号启用、未删除、可调度，并且账号名包含 `-`。
- 某些上游不支持余额接口：加入 `skip_upstreams`，或按需要加入 `newapi_token_upstreams`。
- 倍率监控显示跳过：通常是缺少登录凭据，或站点接口需要认证。
