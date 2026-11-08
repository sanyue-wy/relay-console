# Relay Console

本机运行的 AI 问答控制台：
- 驱动 DeepSeek / 豆包 / Kimi / Gemini / Hark 五个网页版（每个平台用你自己的账号）
- 当前平台触发上限时，用滚动摘要自动接力到下一个平台，并重发这一问
- 对抗模式：网页 vs 网页、网页 vs Agent、Agent vs Agent
- 双池：主池（私有）与对抗池（Agent 经授权可读），两个文件、两把密钥
- MCP：Agent 只能碰对抗池，每次读取都要你在控制台点“批准一次”

## 目录
```
relay-console/
  run_console.py            启动控制台
  config.example.yaml       配置模板（复制为 config.yaml）
  relay/                    控制台后端
    app.py                  FastAPI：/api/*（你）  /agent/*（Agent）
    relay_engine.py         接力引擎 + 可替换路由策略 RelayPolicy
    summarizer.py           滚动摘要 + 交接包
    arena.py                对抗模式
    storage.py              主池 main.db / 对抗池 arena.db / 授权 / 审计
    browser.py              浏览器承载：自建 Chrome 或指纹浏览器（CDP）
    fingerprint.py          指纹浏览器本地 API（AdsPower / ovoBrowser）
    crypto.py               密钥（系统凭据库）
    static/index.html       控制台界面
  adapters/
    spec.py                 适配器规格 v1.0（扩展从这里开始读）
    web_generic.py          通用网页适配器（选择器驱动）
    platforms.py            五个平台
    agent.py                Agent 参与者（经对抗池收发）
    selectors.yaml          各平台选择器与上限文案
  mcp_server/
    server.py               MCP Server（stdio）
    AGENT_README.md         给 Agent 读的说明
  scripts/
    setup_keys.py           保存摘要 API key
    probe.py                探测/修正平台选择器
    fingerprint_profiles.py 列出指纹浏览器环境 ID（填 config 用）
  tests/                    离线自测（无需浏览器）
```

## 安装（Windows）
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml      # 改 chrome_path、Hark 地址、接力顺序、多账号
python scripts/setup_keys.py              # 输入摘要 API key，存入 Windows 凭据管理器
python -m pytest -q                       # 29 项自测应全部通过
```
不需要 `playwright install`，程序连接你本机安装的 Chrome（或指纹浏览器提供的 CDP）。

## 首次使用
1. 逐个平台运行 `python scripts/probe.py deepseek`（kimi / doubao / gemini / hark 同理，多账号加 `--account 账号名`）。
   会弹出该平台专用的 Chrome，**手动登录一次**，之后登录态留在 `profiles/<平台>/<账号>`。
   脚本会打印每个选择器匹配到的数量；为 0 的按页面实际结构改 `adapters/selectors.yaml`。
   加 `--send "你好"` 做一次完整收发测试；输出末尾还会试读侧栏历史会话条数（chat_list_item）。
2. `python run_console.py`，用终端打印的带 token 地址打开控制台。

> selectors.yaml 里的选择器是起步值，网页经常改版，以 probe 结果为准。

## 接力怎么工作
- 每 4 轮（可配）用 API 增量更新一次本会话摘要，加密存主池；未摘要部分超过 `near_threshold_chars` 时也会提前生成（网页看不到额度百分比，以此近似“额度将尽前预生成摘要”的思路）。
- 上限信号三路：页面文案（limit_texts）、网络 429、回复超时/截断（`relay_on_truncation: true` 时截断也接力）。
- 命中后：标记该**平台+账号**冷却 → **先在同平台换下一个可用账号**，没有再按 relay_order 换平台 → 开新对话 → 发送“交接包（结构化摘要 + 最近几轮引用块回放）+ 你刚才那一问”。交接包超过该平台输入上限时改为上传 .md。
- API 不可用时用缓存摘要 + 最近几轮原文，交接不会卡住。

## 对抗模式
- 控制台右栏创建对局：A/B 方填平台名（如 `deepseek`）或 `agent:名称`。
- 每个网页席位有独立私有会话，接力只用自己的摘要；双方只看到对抗池里对方的公开发言。
- Agent 席位：`get_turn` 申请读题 → 你批准 → `fetch_approved` → `submit_turn` 交回。
- `allow_batch_approve: true` 时显示“本轮全部批准”，仍逐条记审计，默认关闭。

## 接入 Agent（MCP）
1. 控制台右栏“新建 Agent 令牌”，令牌只显示一次。
2. 在 Agent 的 MCP 配置里加入：
```json
{
  "mcpServers": {
    "relay-console": {
      "command": "C:/path/to/relay-console/.venv/Scripts/python.exe",
      "args": ["C:/path/to/relay-console/mcp_server/server.py"],
      "env": {
        "RELAY_AGENT_TOKEN": "<控制台给的令牌>",
        "RELAY_CONSOLE_URL": "http://127.0.0.1:8765"
      }
    }
  }
}
```
3. 每个 Agent 一个令牌，可随时吊销；令牌绑定身份，只能看发给它的条目和它所在的对局。
4. Agent 还可申请 `get_session_status`（会话接力链/各平台账号冷却/摘要预览，不含对话正文），同样逐次批准；MCP 只允许连本机回环控制台。

## 多账号与凭据代填（可选）
- `platforms.<pid>.accounts: [main, backup]` 为一个平台配多个账号，每个账号独立 Chrome profile、独立冷却；接力优先在同平台换账号，再换平台。
- 想省掉手动登录：`python scripts/add_credential.py deepseek main` 把该账号的登录凭据存进 Windows 凭据管理器，并给平台开 `autofill: true`。打开平台若未登录会自动代填。
- 凭据只在控制台进程里读出并注入登录表单：不落盘、不进日志、MCP/Agent 永远接触不到；不开 autofill 就完全不用。

## 指纹隔离（可选，P3-7）
默认 `fingerprint.backend: chrome`，每个账号只是独立 Chrome profile（数据隔离，环境指纹仍趋同）。
如果本机装了指纹浏览器，可以让每个“平台+账号”跑在它自己的**环境**里，环境级指纹由它负责：

- 支持 **AdsPower** 与 **ovoBrowser**（两者本地 API 默认都在 `127.0.0.1:50325`，路径也兼容）。
- 本工程不自己实现 Canvas/WebGL/字体等伪装——那层交给指纹浏览器；这里只做“打开环境 → 拿 CDP 端点 → Playwright 接管”。
- 接力换账号时，换的是另一个指纹环境；`close()` 只断开 CDP，不关你的浏览器。

配置步骤：
1. 启动客户端并在设置里开启**本地 API**（AdsPower 还需在 设置 → API 生成 api-key；可填 `fingerprint.api_key`，或用环境变量 `ADSPOWER_API_KEY`）。
2. 在客户端里为每个平台账号建一个环境，**在该环境内手动登录一次**。
3. 列出环境 ID：
   ```powershell
   python scripts/fingerprint_profiles.py --backend ovobrowser
   # AdsPower: python scripts/fingerprint_profiles.py --backend adspower
   ```
4. 写进 `config.yaml`：
   ```yaml
   fingerprint:
     backend: ovobrowser          # 或 adspower
     profiles:
       deepseek: {main: "<环境ID>", alt: "<环境ID>"}
       kimi:     {main: "<环境ID>"}
   ```
   每个启用的平台账号都必须有环境 ID；缺失会直接报错并指出要补的键，不会静默退回 Chrome。

实测（ovoBrowser 0.5.4 + 临时环境）：CDP 接管后 `navigator.webdriver === false`，UA/时区/语言/核心数均来自环境。
注意：指纹伪装由客户端负责，CDP 接管本身仍是可检测信号；两个客户端默认都占 50325，不能同时开启。

## 安全要点
- 不保存任何平台密码；只用你手动登录后留下的 Chrome profile。**例外**：仅当你在 `add_credential` 里主动存入并开启 `autofill` 时，控制台会用凭据库代填登录表单；凭据仍不出本机凭据库、不进任何日志或 MCP 通道。
- 主池、对抗池正文 AES 加密（Fernet），两把密钥存 Windows 凭据管理器。
- 控制台只监听 127.0.0.1；`/api/*` 需要启动时生成的控制台令牌，Agent 令牌无法访问。
- MCP 进程不读任何本地文件，只调用 `/agent/*`；每次读取需新批准，票据一次性、5 分钟过期；每分钟最多 10 次申请；全部写审计。
- **硬隔离（强烈建议）**：如果 Agent 能执行 shell，它在同一 Windows 用户下能读到你的任何文件和凭据库。
  请新建一个 Windows 本地用户（如 `relay`），用它运行控制台和平台 Chrome；
  `data/`、`profiles/` 目录的 NTFS 权限只给 `relay` 用户；Agent 在你的日常用户下运行，只能通过 127.0.0.1 的 MCP 接口访问。
  ```powershell
  icacls data /inheritance:r /grant relay:(OI)(CI)F
  icacls profiles /inheritance:r /grant relay:(OI)(CI)F
  ```
- profiles 所在磁盘建议开启 BitLocker。

## 扩展一个新平台
1. `selectors.yaml` 加一段（url、input、last_reply、limit_texts…）。
2. `adapters/platforms.py`：
```python
@register("newai")
class NewAIAdapter(_Base):
    ID, NAME, MAX_INPUT = "newai", "NewAI", 20000
```
3. `config.yaml` 的 `platforms` 加端口，`relay_order` 按需加入。
4. 页面行为特殊时覆盖 `send / receive / _extract_last_reply / _limit_from_dom` 等钩子。
非网页参与者（例如走官方 API 的模型）直接实现 `adapters/spec.py` 的 `Participant` 即可接入对抗模式。

## 已知限制
- 网页版改版会让选择器失效：`SELECTOR_BROKEN` / `NOT_READY` 错误出现时运行 probe 修正。
- 网页版看不到额度百分比，所以采用滚动摘要 + 字符量阈值（`near_threshold_chars`）而不是阈值预警。
- 网页版没有 system message，交接包作为新对话首条消息发送。
- 请遵守各平台的使用条款；本工程按“每个平台一个本人账号”设计。

## 路线图
与最初问答雏形的逐条对照、尚未实现的功能清单见 [docs/ROADMAP.md](docs/ROADMAP.md)。
