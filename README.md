# BGLab

用你自己的模型，在浏览器里与 AI 玩桌游。

BGLab 是一个本地运行的 AI 桌游平台。接入自己的模型后，就能与 AI 对局，或观看 AI 之间的比赛。棋盘在浏览器中展示，支持中途保存、继续游戏和历史回放。

当前版本 **0.1.1**，主要面向 Windows，以源码形式提供。项目专注于桌游，附带的编程功能仅供尝试。

## 快速开始

需要 [Python 3.11+](https://www.python.org/downloads/windows/)、[Node.js 22.12+](https://nodejs.org/en/download) 和支持工具调用的模型 API Key。安装 Python、Node.js 时将其加入 `PATH`。

下载并解压源码，在包含 `pyproject.toml` 的项目根目录打开 PowerShell，依次执行：

```powershell
# 1. 创建 Python 虚拟环境
python -m venv .venv

# 2. 安装主程序
.\.venv\Scripts\python.exe -m pip install -e .

# 3. 安装并构建游戏前端
npm --prefix games/the-white-castle ci
npm --prefix games/the-white-castle run build

# 4. 启动终端界面
.\.venv\Scripts\bglab.exe
```

**每一步成功后再继续。** 安装完成后，下次只需运行最后一条启动命令。更新源码后，重新安装主程序并构建前端。

<details>
<summary>安装或启动失败？</summary>

先检查主程序是否安装成功：

```powershell
.\.venv\Scripts\python.exe -m pip show bglab
Test-Path .\.venv\Scripts\bglab.exe
```

应显示版本 `0.1.1`，第二条返回 `True`。

- **pip 报 SSL 错误，随后找不到 `hatchling` 等依赖**：先处理到 `pypi.org`、`files.pythonhosted.org` 的连接问题。npm 使用不同的下载服务，它安装成功不代表 pip 的网络正常。若没有网络错误，再检查 Python 与依赖版本。
- **无法识别 `bglab.exe`**：确认当前目录是项目根目录，并用上面的命令检查主程序是否安装成功。
- **构建提示 `/__bglab_shared/` 资源无法打包**：这些资源由 BGLab 本地服务提供。请启动 BGLab 后从游戏入口打开棋盘，不要双击 `dist/index.html`。

已经使用代理软件时，可在当前 PowerShell 为 pip 指定实际的 HTTP 或 mixed 代理地址：

```powershell
$env:PIP_PROXY = Read-Host "请输入实际 HTTP 代理地址（含 http:// 和端口）"
.\.venv\Scripts\python.exe -m pip install -e .
```

地址以自己的代理设置为准，关闭终端后失效。参见 [pip 代理参数](https://pip.pypa.io/en/stable/cli/pip/#cmdoption-proxy)。

</details>

## 配置模型

启动后输入 `/model`，依次选择：

1. 选择“主用”服务商。
2. 选择模型，或填写模型 ID。
3. 核对 API 地址、填写 API Key，点击“保存”。

↑↓ 选择，Tab 切换控件，Enter 确认，Esc 返回。“测试连接”可选，会保存填写的 Key 并发送一次可能计费的请求，之后仍需点击“保存”。

| 服务商 | 配置说明 |
| --- | --- |
| DeepSeek 官方 | 选择 `DeepSeek` → `DeepSeek V4 Flash`，地址保持 `https://api.deepseek.com/v1` |
| OpenCode Go | 选择 `OpenCode Go`，使用对应的地址与 Key |
| 自定义中转 | 选择中转兼容的服务商类型，再填写地址与模型 ID |

参考实测使用 DeepSeek 官方 Flash。不同服务商的协议和参数可能不同，自定义中转请确认支持工具调用。桌游使用“主用”模型，当前不会自动切换备用模型。

## 开始游戏

| 游戏 | 支持人数 |
| --- | --- |
| 姬路城 · The White Castle | 2 人 |
| 花砖物语 · Azul | 2–4 人 |
| 璀璨宝石 · Splendor | 2–4 人 |

输入“我要玩姬路城2人局”，或使用命令 `/bg start 姬路城 human 2` 即可开始。点击“打开棋盘”或按 Ctrl+O，在浏览器中操作自己的回合。

要更改昵称，可在未运行游戏时说“帮我改昵称，我要叫xxx”，之后的新对局会使用这个名字。

### 常用命令

| 命令 | 用途 |
| --- | --- |
| `/help` | 查看帮助 |
| `/model` | 配置模型与 Key |
| `/bg start` | 查看游戏列表与启动命令 |
| `/bg stop` | 停止当前对局，回到终端对话 |
| `/bg resume` | 选择存档，续玩未完成的游戏 |
| `/bg retry` | 从保存的当前局面重试 AI 请求 |
| `/bg replay` | 查看对局回放 |
| `/resume` | 恢复终端对话 |

`/resume` 恢复终端对话，`/bg resume` 恢复游戏。有多份游戏存档时，可以从列表中选择要继续的一局。

## 姬路城表现与费用

姬路城分数约在 **40–50 分**浮动。使用 DeepSeek 官方 Flash，每局每个 AI **闲时约 ¥0.9，高峰时段翻倍**。分数和费用仅供参考，会随局面与模型表现变化；价格以[官方说明](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)为准。

## 工作原理

AI 根据当前局面和游戏策略选择行动，先核验、再提交，由游戏引擎执行并计分。每个 AI 独立保留本局的决策历史，棋盘和存档随行动同步更新。

## 当前局限

- AI 的规划和策略执行仍有波动，偶尔会反复比较、思考过久。
- 已提供自动重试；遇到 API 超时、限流或余额不足，仍可能暂停。服务恢复后可用 `/bg retry` 继续。
- 旧存档不保证跨版本续玩，建议在原版本完成已有对局。
- 当前新局尚未开启游戏聊天、专题 Skills 和长期记忆。

如果遇到问题，欢迎反馈。游戏问题请保留**版本、种子（如有）和存档或回放**，附上操作步骤或截图。种子只能还原开局，中途问题需要对应的行动记录；请勿公开 API Key。

## 后续计划

- **Skills**：让 AI 按局面需要查阅更具体的策略与方法。
- **记忆**：积累对局经验，用于后续游戏。
- **游戏聊天**：支持玩家与 AI 在对局中交流。
- 持续改善决策质量、减少重复与 token 消耗，并简化安装体验。

以上功能将逐步加入，具体时间以版本更新为准。

## 本地数据

存档、对话和设置保存在 `%USERPROFILE%\.bglab`，API Key 保存在本机 `.env`。重新安装不会清除已有存档。模型分析所需的局面和对话会发送到你配置的服务商。

## 许可

代码采用 [MIT 许可](LICENSE.txt)。第三方组件与素材的权利说明见 [THIRD_PARTY_NOTICES.txt](THIRD_PARTY_NOTICES.txt)。
