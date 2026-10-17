# MAA WeCom Bot

一个面向 Windows 的独立 MAA 自动化助手。它通过企业微信智能机器人接收私聊指令，调用本机 MAA / MuMu / ADB 执行一键长草、账号切换和自动肉鸽，并把开始、异常、完成和停止状态推回聊天。

> 本项目是独立社区项目，不修改 MAA 主仓库，也不代表 MAA 官方。当前实现依赖 MAA GUI 的 Windows UI Automation；MAA 版本升级后，极少数按钮或控件名称可能需要同步调整。

## 功能

- 每天 `08:00`、`20:00` 定时清日常，定时前检查 MuMu、MAA 和常驻服务。
- 一次清日常按配置顺序跑完全部账号，每个账号可使用不同理智关卡。
- 每次日常前自动全选任务，并明确关闭自动肉鸽与生息演算。
- 通过企业微信私聊发送 `清日常`、`停止所有任务`、`账号`、`主号`、`状态` 等指令。
- 任务开始、错误日志、完成、停止和账号切换都会推送；完成消息包含账号称呼。
- 自动处理 MAA 公告弹窗：滚动到底部、勾选不再显示、点击确认。
- 登录失效时支持重启游戏、重连和人工校准后的 ADB 手机号密码登录兜底。
- 可配置 DeepSeek 自然语言指令，例如“先清日常，完成后用主号跑肉鸽”。
- GUI 管理机器人、账号、定时、AI、自动肉鸽参数和登录坐标兜底。

## 系统要求

- Windows 10/11。
- Python 3.11–3.13。
- 已安装并能正常手动运行的 MAA，以及其支持的模拟器。默认按 MuMu 12 的 ADB 使用方式设计。
- 企业微信自建应用中的智能机器人，使用 API 模式取得 Bot ID 和 Secret。
- 首次会话 ID 捕获需要你先在手机或桌面企业微信中私聊机器人。

## 安装

```powershell
cd .\maa-wecom-bot
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .\config.example.json .\config.json
.\start_gui.cmd
```

也可以在开发时安装为包：

```powershell
python -m pip install -e ".[dev]"
maa-wecom-bot doctor
```

## 首次配置

1. 在 GUI「机器人」页填写 Bot ID、Secret、MAA 目录、MuMu/ADB 路径。
2. 保存后点击「发送测试消息」；若尚未有会话 ID，先私聊机器人一句话，等待自动捕获。
3. 在「账号」页添加账号名称、登录手机号、密码、MAA 切换值和理智关卡。
4. 在「运行」页点击「更新计划任务」，安装登录常驻服务与定时前置任务。
5. 用 `清日常` 做一次手动验证，再确认每日定时。

不要直接照抄其他人的 `config.json`。仓库只提供不含凭据的 `config.example.json`。

## 常用命令

```powershell
python run.py doctor
python run.py gui
python run.py service
python run.py listen --seconds 120
python run.py send "测试消息"
python run.py status
python run.py ensure
python run.py install-tasks
python run.py setup-maa --times 08:00,20:00
python run.py push-status --dry-run
```

也可以先执行 `python -m pip install -e .`，之后使用 `maa-wecom-bot doctor`。

## 聊天示例

- `清日常`：所有账号依次执行一次。
- `账号`：查看账号列表。
- `主号` / `切换 小号`：切换 MAA 当前使用的账号。
- `肉鸽开始`：用当前账号执行自动肉鸽。
- `肉鸽 主题=萨米 模式=刷等级 难度=15 分队=特训分队`：修改肉鸽参数。
- `状态`：查看当前任务、账号和排队信息。
- `停止所有任务`：停止当前任务并清空排队与临时定时任务。
- `先清日常，完成后用主号跑肉鸽`：需要配置 DeepSeek Key。

## AI 与无 Key 的区别

没有 DeepSeek Key 时，关键字、账号切换、肉鸽参数和状态查询仍然可用；只有组合指令、相对时间和复杂自然语言理解不可用。

配置 Key 后，模型负责把自然语言解析为固定动作 JSON，再由本地代码校验并执行。模型不能直接操作鼠标键盘，也不能绕过源码中的动作白名单。

DeepSeek Key 可在 <https://platform.deepseek.com/api_keys> 创建。默认模型为 `deepseek-flash`，可通过 `deepseek_model` 修改。

## 安全模型

- 首次私聊绑定 `chat_id`；绑定后只处理该会话。
- 群聊控制默认关闭。开启时应同时配置 `allowed_chat_ids`。
- `config.json`、日志、截图、备份和运行状态已加入 `.gitignore`。
- 支持环境变量覆盖敏感值：`MAA_BOT_ID`、`MAA_BOT_SECRET`、`DEEPSEEK_API_KEY`。
- GUI 中 Secret 与密码默认隐藏，但配置文件仍是本机明文；请保护好 Windows 用户目录。

完整说明见 [SECURITY.md](SECURITY.md)。

## 目录结构

```text
src/maa_bot/     Python 包，包含机器人客户端、服务、MAA 操作和 GUI
tools/           MAA 窗口诊断脚本
tests/           不依赖 MAA 的单元测试
run.py           源码目录统一入口，计划任务也调用该文件
config.example.json
```

## 已知限制

- 仅支持 Windows。
- 账号切换依赖 MAA 登录页中已保存的账号；账号密码 ADB 登录需要先完成屏幕坐标校准。
- MAA 或模拟器升级可能改变控件名称、布局或配置文件字段。
- 本项目不是 MAA 官方远控协议实现。若需要跨平台、免 UI 自动化方案，应按 MAA `remote-control-schema` 单独实现协议服务端。
