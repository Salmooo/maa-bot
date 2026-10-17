# 安全说明

本项目会调用本机鼠标、键盘和 ADB，并可能保存游戏账号、企业微信机器人凭据及 DeepSeek API Key。请仅在自己的可信电脑上运行。

## 默认保护

- `config.json`、日志、截图、运行状态和备份均已加入 `.gitignore`。
- 首次私聊会绑定会话 ID；绑定后其他会话不能执行指令或接收通知。
- 群聊控制默认关闭。需要时显式设置 `allow_group_chat` 和 `allowed_chat_ids`。
- AI 只能产生代码白名单中的动作，不能执行任意命令或改写任意文件。
- 机器人通知不会把 Secret 或 API Key 写入日志。

## 凭据建议

- 不要把 `config.json` 发给他人，也不要上传到 Issue。
- Bot Secret 或 DeepSeek Key 一旦泄露，应立刻在对应平台重置。
- 更推荐把敏感值放入环境变量：`MAA_BOT_ID`、`MAA_BOT_SECRET`、`DEEPSEEK_API_KEY`。环境变量会覆盖配置文件中的同名值。
- 公开仓库只提交 `config.example.json`。

如发现安全问题，请通过仓库 Issues 私下提供最少复现信息，不要附带真实凭据或账号密码。
