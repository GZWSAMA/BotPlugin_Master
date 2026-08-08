# Oli Image Generation

Generates and edits images for the configured Oli AstrBot platform through an OpenAI-compatible image API. The plugin keeps a persistent daily limit of 10 images.

## Installation

```bash
cp -r plugins/astrbot_plugin_oli_image_generation /opt/AstrBot/data/plugins/
```

Configure the plugin in AstrBot WebUI or with the generated file:

```text
/opt/AstrBot/data/config/astrbot_plugin_oli_image_generation_config.json
```

Use `config.example.json` as a sanitized reference. Do not commit a real API key.

## Commands

```text
画图 <description>
绘图 <description>
图生图 <description>
改图 <description>
```

The plugin also exposes `oli_generate_image` and `oli_edit_image` LLM tools.

## Daily Quota And Owner Signal

The limit is 10 generated or edited images per day. On an Oli platform message, the plugin resets the current day's quota when the message contains at least:

- 5 commas: `,` or `，`
- 2 periods: `.` or `。`
- 1 exclamation mark: `!` or `！`

The reset is persisted before the plugin sends this normal response and stops further LLM processing for that message:

```text
奥利找到主人啦，今日的图片绘制次数已经重制啦。
```
