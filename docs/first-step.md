# 工单业务接口与完整流程演示

当前版本覆盖登录、创建、列表与详情、状态更新和最终处理结果。所有示例故障与账号均用于个人项目演示，不连接真实故障监控系统。

## 当前接口

- `GET /auth/csrf`：获取 CSRF 令牌并建立匿名会话，返回 `data.csrf_token`。
- `POST /auth/login`：只接受 `username` 和 `password`；携带当前令牌，登录成功返回 `data.user` 和新的 `data.csrf_token`。
- `POST /auth/logout`：携带登录会话和当前令牌，成功返回 `data.logged_out`。
- `POST /tickets`：仅提单人可用，只接受 `title` 和 `description`，成功返回201及 `data` 中的新工单。
- `GET /tickets`：提单人只看自己的列表，处理人可看全部；支持状态筛选和分页。
- `GET /tickets/{id}`：提单人只读自己的工单，处理人可读全部工单。不可见与不存在的工单均返回404。
- `GET /tickets/{id}/history`：沿用工单可见性，查询状态变更的操作人、前后状态、请求编号和时间，按历史编号升序返回 `data.items`、`data.total`。
- `PATCH /tickets/{id}/status`：仅处理人可用；按下面的状态规则更新，成功返回200及完整工单。
- `GET /knowledge/search`、`GET /knowledge/documents/{id}`、`GET /tickets/{id}/knowledge`：本地资料检索、来源原文和工单关联检索。
- `POST /tickets/{id}/answer`：处理人请求可选模型辅助回答；默认关闭，来源及模型验证边界见[知识模块说明](knowledge.md)。

写请求使用 `X-CSRF-Token` 请求头和会话 Cookie。登录后必须使用新令牌。令牌错误返回403；未登录访问工单接口返回401。服务端每次根据用户编号从数据库读取角色。

每次响应通过 `X-Request-ID` 返回服务端生成的32位十六进制请求编号，成功与错误响应都提供；客户端传入的同名请求头不会沿用。编号只放在响应头，原有 JSON 字段保持一致。通过该编号可以在后台关联请求完成日志、500异常日志和状态更新成功日志。响应统一设置 `Cache-Control: no-store`。

标题去首尾空白后为1～100个字符，描述为1～2000个字符，均不自动把数字或数组转换成字符串。NUL 和不能编码为 UTF-8 的文本会被拒绝。请求体上限为64 KiB。

创建、详情、列表条目和状态更新均返回8个工单字段：`id`、`title`、`description`、`creator_id`、`status`、`resolution`、`resolved_by`、`created_at`。初始状态为 `pending`，结果与解决人为空，创建时间使用 UTC。创建时客户端不能指定创建人、状态或角色。

## 列表与状态规则

列表查询只接受 `status`、`limit`、`offset`，未知参数或同名重复参数返回400。`status` 可省略，提供时只接受 `pending`、`in_progress`、`resolved`；`limit` 默认20，范围1～100；`offset` 默认0，范围0～9223372036854775807。两个数值参数只接受不超过19位的 ASCII 十进制数字。

例如 `GET /tickets?status=pending&limit=10&offset=0` 返回 `data.items`、`data.total`、`data.limit`、`data.offset`。`total` 是当前用户可见且符合状态条件的总数，条目按编号降序排列。翻页期间如有新增或状态变化，不保证跨请求的快照一致性。

状态只允许 `pending → in_progress → resolved`，请求体分别为：

```json
{"status": "in_progress"}
```

```json
{"status": "resolved", "resolution": "模拟故障：调整示例超时配置后复测通过。"}
```

解决时结果去首尾空白后必须为1～2000个字符，采用与描述相同的编码校验；`resolved_by` 取当前处理人编号，不接受客户端伪造。缺失、多余或错误类型字段返回400。跳步、回退、重复更新返回409；已解决结果不可再次覆盖。读取旧状态后发生竞争，条件更新未命中的请求也返回409，需重新查询再决定后续操作。

状态、结果、解决人与状态历史在同一事务内保存，数据库错误会一起回滚。历史字段为 `id`、`ticket_id`、`actor_id`、`from_status`、`to_status`、`request_id`、`created_at`；操作人和请求编号由服务端确定。创建工单不记状态变化，旧工单没有记录过的历史不补造。当前只有两个合法推进步骤，无需对历史分页，也未扩展为完整审计系统。

成功提交后日志记录工单编号、操作人编号、旧状态和新状态；历史中的请求编号与本次响应一致。重复、跳步、回退或竞争失败不会增加历史。

## 自动演示

在项目根目录按 README 设置 `$python`，运行：

```powershell
& $python scripts/demo_workflow.py
```

脚本使用临时数据库和三个模拟账号，检查整条工单流程、权限隔离、状态历史、真实知识导入和来源、退出及重新启动后的持久化，并检查每个响应的请求编号和缓存头。模型被显式关闭，脚本还验证资料不足与关闭模式不伪造回答。无需已有开发数据库或正在运行的服务；成功输出17组检查和42次真实 HTTP 请求，失败以非零退出码结束。

## PowerShell 演示

先按根目录说明初始化账号并启动服务。在另一个 PowerShell 终端运行下面的请求。这组手工演示会向开发数据库写入一条模拟工单；密码使用初始化时自行设置的值，不写进脚本。

```powershell
$baseUrl = 'http://127.0.0.1:5000'
$csrfResult = Invoke-RestMethod -Uri "$baseUrl/auth/csrf" -SessionVariable ticketSession
$demoPassword = Read-Host '演示密码' -AsSecureString
$loginBody = @{
    username = 'submitter_a'
    password = [System.Net.NetworkCredential]::new('', $demoPassword).Password
} | ConvertTo-Json
$loginResult = Invoke-RestMethod -Method Post -Uri "$baseUrl/auth/login" `
    -WebSession $ticketSession -Headers @{'X-CSRF-Token' = $csrfResult.data.csrf_token} `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($loginBody))
Remove-Variable loginBody, demoPassword

$ticketBody = @{
    title = '模拟接口超时'
    description = '模拟故障：调用接口超时，等待人工排查。'
} | ConvertTo-Json
$createdTicket = Invoke-RestMethod -Method Post -Uri "$baseUrl/tickets" `
    -WebSession $ticketSession -Headers @{'X-CSRF-Token' = $loginResult.data.csrf_token} `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($ticketBody))
$createdTicket.data
Invoke-RestMethod -Uri "$baseUrl/tickets/$($createdTicket.data.id)" -WebSession $ticketSession
Invoke-RestMethod -Uri "$baseUrl/tickets?status=pending&limit=10&offset=0" -WebSession $ticketSession
```

继续登录处理人，使用独立会话和登录后新令牌完成处理：

```powershell
$handlerCsrf = Invoke-RestMethod -Uri "$baseUrl/auth/csrf" -SessionVariable handlerSession
$demoPassword = Read-Host '处理人演示密码' -AsSecureString
$handlerLoginBody = @{
    username = 'handler'
    password = [System.Net.NetworkCredential]::new('', $demoPassword).Password
} | ConvertTo-Json
$handlerLogin = Invoke-RestMethod -Method Post -Uri "$baseUrl/auth/login" `
    -WebSession $handlerSession -Headers @{'X-CSRF-Token' = $handlerCsrf.data.csrf_token} `
    -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($handlerLoginBody))
Remove-Variable handlerLoginBody, demoPassword

$statusUrl = "$baseUrl/tickets/$($createdTicket.data.id)/status"
$handlerHeaders = @{'X-CSRF-Token' = $handlerLogin.data.csrf_token}
$progressBody = @{status = 'in_progress'} | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri $statusUrl -WebSession $handlerSession `
    -Headers $handlerHeaders -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($progressBody))
$resultBody = @{
    status = 'resolved'
    resolution = '模拟故障：调整示例超时配置后复测通过。'
} | ConvertTo-Json
Invoke-RestMethod -Method Patch -Uri $statusUrl -WebSession $handlerSession `
    -Headers $handlerHeaders -ContentType 'application/json; charset=utf-8' `
    -Body ([System.Text.Encoding]::UTF8.GetBytes($resultBody))

# 原提单人仍用自己的会话读取最终结果。
Invoke-RestMethod -Uri "$baseUrl/tickets/$($createdTicket.data.id)" -WebSession $ticketSession
```

## 错误约定

失败响应包含 `error.code` 和 `error.message`，不同时返回成功的 `data`。

- 400 `INVALID_REQUEST`：字段缺失、多余、类型或长度错误，或 JSON 无法处理。
- 401 `AUTH_REQUIRED`：尚未登录；401 `INVALID_CREDENTIALS`：登录账号或密码错误。
- 403 `FORBIDDEN`：角色不允许；403 `CSRF_FAILED`：令牌校验失败。
- 404 `NOT_FOUND`：工单不存在或当前用户不可见。
- 405 `METHOD_NOT_ALLOWED`：当前接口不支持该方法。
- 409 `INVALID_TRANSITION`：状态流转不允许，或竞争更新时原状态已改变。
- 409 `KNOWLEDGE_CHANGED`：生成回答期间来源已更新，旧回答被弃用。
- 429 `MODEL_BUDGET_EXHAUSTED`：本轮模型调用预算或次数已用完。
- 413 `REQUEST_TOO_LARGE`：请求体过大；415 `UNSUPPORTED_MEDIA_TYPE`：没有按 JSON 发送。
- 500 `INTERNAL_ERROR`：意外的服务端错误；详细异常仅记入后台日志。
- 模型接口另有502、503、504分支，见[知识模块说明](knowledge.md)，不当作业务登录失败。
