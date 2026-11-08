# Relay Console

本机运行的 AI 问答控制台：把多个网页版 AI 的免费额度串成一条不断线的会话。

- **多平台接力**：驱动 DeepSeek / 豆包 / Kimi / Gemini / Hark 五个网页版；当前平台触发上限时，用滚动摘要自动接力并把刚才那一问重发过去
- **平台 + 账号两级轮换**：每个平台可登记多个账号（独立 profile / 独立指纹环境 / 独立冷却），接力先在同平台换账号，再跨平台
- **无缝衔接**：结构化交接摘要（目标约束 / 已确认事实 / 进度 / 待决问题）+ 最近几轮引用块回放（ghost messages），API 不可用时用缓存摘要照常交接
- **对抗模式**：网页 vs 网页、网页 vs Agent、Agent vs Agent
- **双池与授权**：主池（私有）与 Agent 读取池（对抗池）两个文件、两把密钥；会话池与读取池一一对应；Agent 每次读取都要你在控制台批准
- **MCP 接入**：Agent 只能碰读取池，票据一次性、全程审计
- **指纹承载**：自建 Chrome 或外部指纹浏览器（AdsPower / ovoBrowser），每个平台账号一个环境
- **四页控制台**：开始（会话）/ Agent 池 / 账户 / 设置，modern-gradient 风格

## 目录
```
relay-console/
  run_console.py            启动控制台
  启动控制台.bat            双击快速启动（源码模式）
  config.example.yaml       配置模板（复制为 config.yaml）
  relay/                    控制台后端
    app.py                  FastAPI：/api/*（你）  /agent/*（Agent）
    relay_engine.py         接力引擎 + 可替换路由策略 RelayPolicy
    summarizer.py           滚动摘要 + 交接包（ghost messages）
    arena.py                对抗模式
    storage.py              主池 main.db / 读取池 arena.db / 授权 / 审计
    browser.py              浏览器承载：自建 Chrome 或指纹浏览器（CDP）
    fingerprint.py          指纹浏览器本地 API（AdsPower / ovoBrowser）
    crypto.py               密钥与凭据（系统凭据库）
    config.py               配置加载 / 平台账号表 / 写回 config.yaml
    static/index.html       控制台界面（四页 SPA）
  adapters/
    spec.py                 适配器规格 v1.0（扩展从这里开始读）
    web_generic.py          通用网页适配器（选择器驱动 + 会话发现 + 登录代填）
    platforms.py            五个平台
    agent.py                Agent 参与者（经读取池收发）
    selectors.yaml          各平台选择器与上限文案
  mcp_server/
    server.py               MCP Server（stdio，仅允许连本机回环控制台）
    AGENT_README.md         给 Agent 读的说明
  scripts/
    setup_keys.py           保存摘要 API key
    probe.py                探测/修正平台选择器（支持 --account）
    add_credential.py       录入平台账号登录凭据（autofill 用）
    fingerprint_profiles.py 列出指纹浏览器环境 ID（填 config 用）
    build_exe.py            打包 Windows exe（PyInstaller，分发用）
    make_shortcut.py        生成桌面/开始菜单快捷方式（免注册表）
    create_desktop_shortcut.bat  exe 接收者用的快捷方式脚本（随包分发）
  tests/                    离线自测（无需浏览器），35 项
```

## 安装（Windows）
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy config.example.yaml config.yaml      # 改 chrome_path、Hark 地址、接力顺序、多账号
python scripts/setup_keys.py              # 输入摘要 API key，存入 Windows 凭据管理器
python -m pytest -q                       # 35 项自测应全部通过
```
不需要 `playwright install`，程序连接你本机安装的 Chrome（或指纹浏览器提供的 CDP）。

## 快速启动（免注册表）
- **源码运行**：双击根目录的 **`启动控制台.bat`**；或运行 `python scripts/make_shortcut.py`
  生成「桌面 + 开始菜单」快捷方式（只写 `.lnk` 文件——不写注册表、不装服务、不设自启，
  `--remove` 两步删除）。控制台窗口保持打开就是服务在跑，关窗即停止。
- **exe 接收者**：程序目录里有 **`创建桌面快捷方式.bat`**，双击即得桌面快捷方式。
- 想钉到任务栏：开始菜单搜 Relay Console → 右键 → 固定到任务栏（系统自带操作，同样不碰注册表）。

## 首次使用
1. **登录各平台**，二选一：
   - **自建 Chrome（默认）**：逐个平台运行 `python scripts/probe.py deepseek`（kimi / doubao / gemini / hark 同理，多账号加 `--account 账号名`），会弹出该平台专用的 Chrome，**手动登录一次**，登录态留在 `profiles/<平台>/<账号>`。
   - **指纹浏览器**：见下文[指纹隔离](#指纹隔离可选p3-7)，在每个环境里手动登录一次。
2. probe 会打印每个选择器匹配到的数量；为 0 的按页面实际结构改 `adapters/selectors.yaml`。
   加 `--send "你好"` 做一次完整收发测试；输出末尾还会试读侧栏历史会话条数（chat_list_item）。
3. `python run_console.py`，用终端打印的带 token 地址打开控制台（token 每次启动重新生成）。

> selectors.yaml 里的选择器是起步值，网页经常改版，以 probe 结果为准。

## 控制台界面
modern-gradient 风格（stylekit），单文件 SPA 四页切换：
- **开始**：只负责会话——新建、列表、问答（Ctrl+Enter 发送），接力提示内联显示在对话里。
- **Agent 池**：授权请求（3 秒自动刷新）、发布到读取池、池条目（显示源会话）、Agent 令牌、对抗对局、审计。
- **账户**：平台多账号池登记——增删账号、autofill 开关、打开/登录、平台历史会话一键接入。
- **设置**：接力顺序 / 冷却分钟 / 截断接力 / 预生成阈值 / 摘要轮数 / 对抗参数，保存即生效并写回 `config.yaml`（整体落盘，yaml 注释不保留）。

## 接力怎么工作
- 每 4 轮（可配）用 API 增量更新一次本会话摘要，加密存主池；未摘要部分超过 `near_threshold_chars` 时也会提前生成（网页看不到额度百分比，以此近似"额度将尽前预生成摘要"的思路）。
- 上限信号三路：页面文案（limit_texts）、网络 429、回复超时/截断（`relay_on_truncation: true` 时截断也接力）。
- 命中后：标记该**平台+账号**冷却 → **先在同平台换下一个可用账号**，没有再按 relay_order 换平台 → 开新对话 → 发送"交接包（结构化摘要 + 最近几轮引用块回放）+ 你刚才那一问"。交接包超过该平台输入上限时改为上传 .md。
- API 不可用时用缓存摘要 + 最近几轮原文，交接不会卡住。

## 多账号池与凭据代填
- `platforms.<pid>.accounts: [main, backup]` 为一个平台登记多个账号（也可在控制台**账户页**操作，立即生效并写回 config.yaml）。每个账号独立 Chrome profile（或独立指纹环境）、独立冷却。
- 想省掉手动登录：`python scripts/add_credential.py deepseek main` 把该账号的登录凭据存进 Windows 凭据管理器，并给平台开 `autofill: true`（账户页可勾选）。打开平台若未登录会自动代填。
- 凭据只在控制台进程里读出并注入登录表单：不落盘、不进日志、MCP/Agent 永远接触不到；不开 autofill 就完全不用。

## 指纹隔离（可选，P3-7）
默认 `fingerprint.backend: chrome`，每个账号只是独立 Chrome profile（数据隔离，环境指纹仍趋同）。
如果本机装了指纹浏览器，可以让每个"平台+账号"跑在它自己的**环境**里，环境级指纹由它负责：

- 支持 **AdsPower** 与 **ovoBrowser**（两者本地 API 默认都在 `127.0.0.1:50325`，路径也兼容）。
- 本工程不自己实现 Canvas/WebGL/字体等伪装——那层交给指纹浏览器；这里只做"打开环境 → 拿 CDP 端点 → Playwright 接管"。
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

## 会话池 ↔ Agent 读取池
- **一一对应**：带会话的发布（最新摘要 / 最近消息 / 自定义文本）总是原位更新同一条目（`items.session_id`），不产生重复条目；不带会话的发布是独立条目。会话详情与池条目互相显示关联。
- **对抗模式**：控制台"Agent 池"页创建对局，A/B 方填平台名（如 `deepseek`）或 `agent:名称`。每个网页席位有独立私有会话，接力只用自己的摘要；双方只看到池里对方的公开发言。
- **Agent 席位流程**：`get_turn` 申请读题 → 你批准 → `fetch_approved` → `submit_turn` 交回。`allow_batch_approve: true` 时显示"本轮全部批准"，仍逐条记审计，默认关闭。
- **跨对话扩展点**：条目已支持 audience 定向与一次申请多条，后续做"跨会话汇总读取"只需在发布端聚合多个会话内容或放开逐条授权，不动授权模型。

## 接入 Agent（MCP）
1. 控制台"Agent 池"页 → 新建 Agent 令牌（只显示一次）。
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

Agent 侧工具一览（详细规则见 [mcp_server/AGENT_README.md](mcp_server/AGENT_README.md)）：
| 工具 | 作用 | 授权 |
|---|---|---|
| `list_pool()` | 可见条目 ID 与标题，不含内容 | 免授权 |
| `request_read(item_ids, reason)` | 申请读取池条目 | 逐次批准 |
| `get_turn(arena_id, reason)` | 申请读取对局本轮题目 | 逐次批准 |
| `get_session_status(session_id, reason)` | 会话接力链 / 各平台账号冷却 / 摘要预览（不含对话正文） | 逐次批准 |
| `check_request(request_id)` | 查询申请状态 | — |
| `fetch_approved(request_id)` | 取回已批准内容（一次性票据，5 分钟有效） | — |
| `submit_turn(arena_id, content)` | 提交本轮发言 | 须先读到题目 |

## 安全要点
- 平台密码默认不保存，只用你手动登录后留下的 profile。**例外**：仅当你在 `add_credential` 里主动存入并开启 `autofill` 时，控制台会用凭据库代填登录表单；凭据仍不出本机凭据库、不进任何日志或 MCP 通道。
- 主池、读取池正文 AES 加密（Fernet），两把密钥存 Windows 凭据管理器。
- 控制台只监听 127.0.0.1；`/api/*` 需要启动时生成的控制台令牌，Agent 令牌无法访问。
- MCP 进程不读任何本地文件，只调用 `/agent/*`，且只允许连本机回环地址（防 SSRF）；每次读取需新批准，票据一次性、5 分钟过期；每分钟最多 10 次申请；全部写审计。
- **硬隔离（强烈建议）**：如果 Agent 能执行 shell，它在同一 Windows 用户下能读到你的任何文件和凭据库。
  请新建一个 Windows 本地用户（如 `relay`），用它运行控制台和平台浏览器；
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

## 打包为 exe 分发（可选）
**程序与数据分离**：源码运行时数据在仓库根（`config.yaml` / `data/` / `profiles/`）；exe 运行时
数据统一放在 `%LOCALAPPDATA%\RelayConsole`，也可用环境变量 `RELAY_HOME` 指到任意目录
（如 `D:\RelayData`）。程序目录可随时整体删除/替换，数据不受影响。

打包（本机需要 Python）：
```powershell
pip install pyinstaller
python scripts/build_exe.py
# 产物：dist\RelayConsole\（程序目录）
#       dist\RelayConsole-<版本>-win64.zip（发给别人的压缩包）
```

接收者使用：解压 zip → 双击 `RelayConsole.exe` → 首次运行自动在数据根生成
`config.yaml`、`data/`、`profiles/`；启动日志与数据根下的 `console_url.txt` 里都有带
token 的控制台地址（token 每次启动重新生成）。之后按「首次使用」登录各平台即可。

**升级继承**：用新版本程序目录覆盖旧的即可——数据根本来就不在程序目录里，旧会话、
读取池、登录态全部保留。三层保障：数据库缺列自动迁移；老 `config.yaml` 缺失的新键
自动从模板补齐（只补缺、不覆盖已有值）；密钥存 Windows 凭据管理器，不随程序目录走。

## 已知限制
- 网页版改版会让选择器失效：`SELECTOR_BROKEN` / `NOT_READY` 错误出现时运行 probe 修正。
- 网页版看不到额度百分比，所以采用滚动摘要 + 字符量阈值（`near_threshold_chars`）而不是阈值预警。
- 网页版没有 system message，交接包作为新对话首条消息发送。
- 平台会话列表（chat_list_item）与登录表单选择器因页面而异，需按 probe 结果逐平台配置。
- 各平台的使用条款与账号政策请自行确认；指纹层如需隔离，交给外部指纹浏览器承担（见「指纹隔离」）。

## 路线图
与最初问答雏形的逐条对照、实现状态与扩展点见 [docs/ROADMAP.md](docs/ROADMAP.md)。
