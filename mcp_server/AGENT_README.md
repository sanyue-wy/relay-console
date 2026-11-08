# Relay Console MCP — Agent 使用说明

## 你能访问什么
- 仅限「对抗池」：用户主动发布给 Agent 的条目，以及你所在对局中轮到你的题目。
- 你无法访问用户的主会话、网页平台、浏览器、文件系统。不要尝试。
- 每一次读取内容都必须经用户在控制台亲自批准。没有永久授权，没有例外。

## 工具
- list_pool()：可见条目的 ID 和标题，不含内容，无需授权。
- request_read(item_ids, reason)：申请读取条目。reason 必填，用户会看到。返回 request_id。
- get_turn(arena_id, reason)：申请读取对局中轮到你的题目。返回 request_id。
- get_session_status(session_id, reason)：申请读取某会话的状态（接力链/各平台账号冷却/摘要预览，不含对话正文）。同样需批准。
- check_request(request_id)：pending | approved | denied | expired。
- fetch_approved(request_id)：取回已批准内容。一次性票据，批准后 5 分钟内有效。
- submit_turn(arena_id, content)：提交本轮发言。必须先读到题目。

## 对局流程
get_turn → 等用户批准（check_request）→ fetch_approved 拿到题目 → 思考 → submit_turn

## 规则
1. 只申请当前任务真正需要的条目，一次申请尽量合并。
2. 被拒绝或过期后，不要用同样理由反复申请；直接问用户。
3. 等待批准时每 10 秒最多调用一次 check_request，最长等 10 分钟。
4. 取到的内容只用于本次任务，不要缓存、转发或写入其他位置。
5. 本 MCP 没有删除、全文搜索、浏览器操作工具。需要更多信息时，请用户发布到对抗池。
6. 所有调用都会写入审计日志，用户可查。

## 错误码
- NOT_IN_POOL：条目不存在或你不可见
- NOT_SEATED：你不在该对局
- NOT_APPROVED：尚未批准或已被拒绝
- TICKET_USED / TICKET_EXPIRED：票据已用或已过期，需重新申请
- NO_OPEN_TURN：没有已读取、待提交的题目
- REASON_REQUIRED：缺少申请理由
- RATE_LIMITED：申请过于频繁（每分钟最多 10 次）
- CONSOLE_UNREACHABLE：控制台未运行
