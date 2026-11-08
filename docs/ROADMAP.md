# 问答雏形对照与待开发功能

来源：`C:\Users\wy\Desktop\问答.doc`（需求问答雏形，2026-11-07）。本文把雏形里的每一条要求
与本工程现状逐一对照，并整理出**尚未实现的功能清单**（按优先级）。

## 一、逐条对照

| 雏形要求 | 工程现状 | 状态 |
|---|---|---|
| 一个 MCP 通向浏览器控制台，管理问答会话 | 已实现，且做了职责分离：浏览器控制台归你（/api/*），MCP 只通向对抗池（/agent/*），Agent 接触不到浏览器和主会话 | ✅（安全加强版） |
| 免费额度用完后自动换下一个网页版 | 接力引擎：DOM 文案 / 网络 429 / 超时截断三路信号 → 冷却 → 按 relay_order 换平台 | ✅ |
| 内置 API 做会话总结，方便交接 | Summarizer：每 N 轮 + 字符量阈值滚动更新，AES 加密存主池 | ✅ |
| 下一个网页版无缝衔接 | 交接包（四段结构化摘要 + 最近几轮原文）作为新会话首条消息，重发当前问题；超长自动改上传 .md | ✅ |
| 额度检测①UI 文案（多语言） | selectors.yaml `limit_texts` | ✅（多语言覆盖有限） |
| 额度检测②网络 429 拦截 | 页面 response 监听 + `limit_status_codes` | ✅ |
| 额度检测③主动阈值（85% 预生成） | 网页看不到额度百分比 → 用「未摘要字符量 ≥ near_threshold_chars」近似；摘要本就每 4 轮预生成并缓存，切换时不依赖 API | ✅（近似实现） |
| 摘要结构：目标约束 / 已确认事实 / 进度 / 待决问题 | SUMMARY_PROMPT 四小节硬性要求 | ✅ |
| 摘要作为 **system message** 注入 | 网页版没有 system message 接口，以首条用户消息替代 | ⚠️ 结构性限制 |
| Ghost messages：旧消息以内联原生样式展示 | 交接包最近几轮以 Markdown 引用块回放（`> **用户**：…`） | ✅ |
| 多账号 cookie 池、同平台 `switch_account` 轮换 | 每平台多账号（`platforms.<pid>.accounts`），接力优先同平台换账号；账号隔离用独立 profile 或指纹环境 | ✅ |
| MCP 暴露 `get_account_status()` / `list_conversations()` 等编排工具 | 有意收窄：只给 `get_session_status`（经对抗池授权通道），Agent 不操控浏览器 | ✅（安全收窄） |
| 平台侧历史会话列表，选择交接源会话 | 适配器 `list_chats()` + 控制台 `/api/platforms/{pid}/chats` + `/api/sessions/resume` | ✅ |
| 凭据 vault、Agent 零明文访问 | 更强：根本不存密码，手动登录留 Chrome profile；池密钥存 Windows 凭据管理器 | ✅（另辟蹊径） |
| storageState AES-256-GCM 加密落盘 | 双池正文 Fernet(AES) 加密；Chrome profile 本身未加密，靠 NTFS 权限 + BitLocker 建议 | ⚠️ 部分 |
| 语义受限高层工具、审批门控、作用域限制 | MCP 六个语义化工具 + 逐次批准 + 一次性票据 + 审计 | ✅ |
| 持久化 profile 预就绪、切换零冷启动 | 每平台独立持久 profile，可预先「打开/登录」 | ✅ |
| 顺序交接：同一时刻只有一个账号持有写入权 | 对抗模式每席位私有会话；同平台页面 asyncio.Lock 串行 | ✅ |
| 浏览器指纹隔离 / 反检测（防关联） | 已实现（复用本机指纹浏览器）：`fingerprint.backend: adspower / ovobrowser` 时每个“平台+账号”跑在独立指纹环境里；本工程只调本地 API 开环境 + CDP 接管，不自己造指纹层 | ✅ |
| conversationId 继续已有对话 | chat_ref + Cap.RESUME_URL 回旧会话 | ✅ |

## 二、待开发清单（2026-11-08 更新：P1/P2/P3 已完成）

### P1 — 直接对应雏形核心设想 ✅ 已实现
1. **同平台多账号池与账号级轮换** ✅
   `platforms.<pid>.accounts: [main, backup]`；每账号独立 Chrome profile（`profiles/<平台>/<账号>`）
   与独立调试端口、独立冷却；接力优先在同平台换账号，全部账号不可用再跨平台。
   会话记录当前账号（`sessions.account`），接力链只在跨平台时追加。
2. **平台侧会话发现（list_conversations）** ✅
   适配器 `list_chats()`（selectors.yaml `chat_list_item`，probe 会试读并报告条数）；
   控制台 `GET /api/platforms/{pid}/chats` + `POST /api/sessions/resume` 把历史会话接进来。
3. **MCP 会话状态工具（雏形的 get_account_status）** ✅
   MCP 新增 `get_session_status(session_id, reason)`：走对抗池授权通道的第三种作用域
   `session`，逐次批准 + 一次性票据；只返回接力链/各平台账号冷却/摘要预览（前 600 字），
   对话正文永不出主池（测试断言）。

### P2 — 体验补全 ✅ 已实现
4. **Ghost messages 内联展示** ✅ 交接包的最近几轮改为 Markdown 引用块回放（`> **用户**：…`）。
5. **limit_texts 多语言扩展** ✅ DeepSeek/豆包/Kimi 补英文文案；probe 可核查。
6. **截断回复的接力策略开关** ✅ `relay_on_truncation: true` 时回复被截断（非上限）也触发接力。

### P3 — 凭据代填、指纹隔离均已实现
7. **浏览器指纹隔离 / 反检测（防关联）** ✅ 已实现（2026-11-08）。
   设计决定：**不自己造指纹引擎**，复用本机已装的指纹浏览器（AdsPower / ovoBrowser）——
   环境级指纹（UA、时区、语言、屏幕、Canvas/WebGL、字体、WebRTC、硬件参数…）由它们负责，
   本工程只做三件事：调本地 API 打开指定环境 → 拿到 CDP 端点 → Playwright 接管页面。
   - `relay/fingerprint.py`：AdsPower（`/api/v1/browser/start`，`api-key` 头）与
     ovoBrowser（`/browser/open`，POST JSON）两个后端；两者默认都监听 127.0.0.1:50325。
   - `relay/browser.py`：`fingerprint.backend` 选承载方式；每个“平台+账号”映射一个环境 ID，
     接力换账号时换的是另一个指纹环境；缺失映射直接报错，不静默退回 Chrome。
   - `scripts/fingerprint_profiles.py`：列出环境 ID，方便填 `fingerprint.profiles`。
   - 实测：ovoBrowser 0.5.4 + 临时环境，CDP 接管后 `navigator.webdriver === false`，
     UA/时区/语言/核心数来自环境。
   已知边界：CDP 接管本身仍是可检测信号（指纹伪装不等于不可检测）；两个客户端默认同端口，
   不能同时开启；各平台风控持续变化，不承诺“不被关联”，只提供环境级隔离这一层。
8. **凭据（密码）代填** ✅ 已按护栏版实现（2026-11-08）：凭据只存 Windows 凭据管理器
   （`scripts/add_credential.py` 主动录入），仅控制台进程在打开平台且未登录时读取并注入
   登录表单；不落盘、不进日志、MCP/Agent 永远接触不到；默认关闭（`autofill: true` 才启用）。
   原“零明文底线”修正为：Agent/MCP 零接触，用户对自己账号的凭据可自主选择托管。

## 三、与雏形的两处有意分歧（设计决定，非遗漏）

1. **MCP 不是控制平面，而是最小授权通道。** 雏形设想 MCP 暴露 `switch_account` 等
   编排工具直接驱动浏览器；工程改为：浏览器只受控制台驱动，Agent 只经对抗池
   读写，每次读取都要人批准。若 Agent 本身可信度不足，这比雏形更安全。
2. **不管理账号密码。** 雏形参考 psamvault / moonlighting 做“凭据 broker 注入”；
   工程选择根本不碰密码（手动登录 + 持久 profile），凭据面为零，broker 层可省。

## 四、代码审查修复（2026-11-08，基于 a81e873）

### 已修复
- **XSS**：`esc()` 补 `"` / `'`；所有动态内容转义；前端逻辑外置到 `static/app.js`，
  删除全部内联 `onclick`，改用 `data-act` 事件委托；响应加 `script-src 'self'` CSP、
  `X-Frame-Options`、`nosniff`、`no-referrer`。已用真实浏览器验证：标题注入
  `"><img onerror=...>` 只显示为文本，脚本不执行。
- **调试端口冲突**：端口改为 `base + 10*账号序号`（每平台最多 10 个账号），
  不再与相邻平台 base 相撞；浏览器锁改为每个“平台+账号”一把。
- **按账号冷却**：只有平台下所有账号都冷却才跳过该平台，其余账号仍可接力。
- **Agent 身份**：`create_agent` 重名报 `AGENT_EXISTS`，不再 `INSERT OR REPLACE` 复活
  已吊销身份；名字限制中英文/数字/`_ . -`，防逗号破坏 audience、引号破坏前端。
- **授权 TOCTOU**：发布时记录内容哈希；批准时把哈希写入票据；fetch 时不一致返回
  `CONTENT_CHANGED`；fetch 时重新校验 audience。
- **接力**：`new_chat + 交接包 + send + receive` 在同一把锁内；`is_handoff` 显式标志；
  超长交接包保留头部/摘要、只裁回放；`NotReady/SelectorBroken` 也尝试下一个目标；
  全部失败回滚已入库的用户消息；后台任务保留引用并取回异常。
- **控制台**：bootstrap token 一次性（换 HttpOnly cookie 后作废）；TrustedHost 只允许
  本机 Host；`KeyError` → 404；对局任务取消不再抛 `CancelledError`；对局中途失败回滚
  本轮发言；429 匹配改为精确主机/子域。
- **存储**：`messages(limit)` 在 SQL 层截取 + `(session_id,id)` 索引；开启 WAL；
  `items.session_id` 唯一索引 + 单事务发布；凭据改存 JSON（用户名含冒号不再解析错）；
  `save_config` 临时文件 + `os.replace` 原子替换，并把数据路径写回相对形式。
- **工程**：`Config.__getattr__` 抛 `AttributeError`；`requirements-dev.txt` 拆分
  pytest/pywin32/pyinstaller；`RelayConsole.spec` 不再入库（由 build_exe 现场生成）。
- **托盘（2026-11-08 追加）**：`run_console.py` 启动后在 Windows 右下角显示托盘图标；
  双击/“打开控制台”生成新的一次性地址；右键“重启控制台”在原进程内重新加载
  `config.yaml` 与 `relay.app`（不用 `os.execv`——本机带空格路径下实测会拼坏 argv）；
  “退出”停服务。依赖 `pystray` + `Pillow`，缺失时自动跳过托盘。

### 本轮未做（记录取舍）
- **异步路径里的同步 sqlite**：改为 `asyncio.to_thread` / aiosqlite 会牵动整个存储层
  调用形态，收益主要是高并发下的事件循环延迟；当前单用户控制台优先级低。
- **app import 时加载配置/建目录**：改成 app factory 需要重排模块级路由与测试注入方式，
  留待下一次结构调整。
- **冷却持久化**：重启后冷却丢失，目前可用配置里的默认冷却时间兜底。
- **官方 API 适配器**：审查建议给 DeepSeek/Kimi/Gemini 写官方 API 参与者。这是新功能
  方向（`Participant` 规格已支持），不属于本轮缺陷修复；需要 API key 与计费决策，
  单独立项。
