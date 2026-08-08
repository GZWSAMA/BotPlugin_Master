# 奥利绘图插件

通过 OpenAI 兼容图片接口为配置的奥利 AstrBot 平台生成图片或图生图。插件会持久化每日 10 张的绘图额度。

## 安装

```bash
cp -r plugins/astrbot_plugin_oli_image_generation /opt/AstrBot/data/plugins/
```

在 AstrBot WebUI 或生成的配置文件中填写接口参数：

```text
/opt/AstrBot/data/config/astrbot_plugin_oli_image_generation_config.json
```

请以 `config.example.json` 为参考，不要提交真实 API key。

## 命令

```text
画图 <描述>
绘图 <描述>
图生图 <描述>
改图 <描述>
```

也可以通过 `oli_generate_image` 与 `oli_edit_image` LLM 工具调用。

## 每日额度与主人信号

每日生成或编辑图片的总额度为 10 张。奥利平台收到一条消息时，只要其中至少包含以下标点，就会重置当天额度：

- 5 个逗号：`,` 或 `，`
- 2 个句号：`.` 或 `。`
- 1 个感叹号：`!` 或 `！`

重置会先持久化成功，再作为普通回复发送下面的消息，并阻止该消息继续进入 LLM：

```text
奥利找到主人啦，今日的图片绘制次数已经重制啦。
```
