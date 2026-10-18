# 登录和创建工单的接口与演示

本小步只覆盖登录、创建工单和查询详情。所有示例故障与账号均用于个人项目演示。

## 当前接口

- `GET /auth/csrf`：获取 CSRF 令牌并建立匿名会话，返回 `data.csrf_token`。
- `POST /auth/login`：只接受 `username` 和 `password`；携带当前令牌，登录成功返回 `data.user` 和新的 `data.csrf_token`。
- `POST /auth/logout`：携带登录会话和当前令牌，成功返回 `data.logged_out`。
- `POST /tickets`：仅提单人可用，只接受 `title` 和 `description`，成功返回201及 `data` 中的新工单。
- `GET /tickets/{id}`：提单人只读自己的工单，处理人可读全部工单。不可见与不存在的工单均返回404。

写请求使用 `X-CSRF-Token` 请求头和会话 Cookie。登录后必须使用新令牌。令牌错误返回403；未登录访问工单接口返回401。服务端每次根据用户编号从数据库读取角色。

标题去首尾空白后为1～100个字符，描述为1～2000个字符，均不自动把数字或数组转换成字符串。NUL 和不能编码为 UTF-8 的文本会被拒绝。请求体上限为64 KiB。

工单返回字段为 `id`、`title`、`description`、`creator_id`、`status`、`created_at`。初始状态固定为 `pending`，时间使用 UTC；客户端不能指定创建人、状态或角色。

## PowerShell 演示

先按根目录说明初始化账号并启动服务。在另一个 PowerShell 终端运行下面的请求。演示密码使用初始化时自行设置的值，不写进脚本。

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
```

## 错误约定

失败响应包含 `error.code` 和 `error.message`，不同时返回成功的 `data`。

- 400 `INVALID_REQUEST`：字段缺失、多余、类型或长度错误，或 JSON 无法处理。
- 401 `AUTH_REQUIRED`：尚未登录；401 `INVALID_CREDENTIALS`：登录账号或密码错误。
- 403 `FORBIDDEN`：角色不允许；403 `CSRF_FAILED`：令牌校验失败。
- 404 `NOT_FOUND`：工单不存在或当前用户不可见。
- 405 `METHOD_NOT_ALLOWED`：当前接口不支持该方法。
- 413 `REQUEST_TOO_LARGE`：请求体过大；415 `UNSUPPORTED_MEDIA_TYPE`：没有按 JSON 发送。
- 500 `INTERNAL_ERROR`：意外的服务端错误；详细异常仅记入后台日志。

状态冲突409属于后续状态更新接口，本小步尚未提供该接口。

## 读代码的顺序

从 `ticket_backend/tickets.py` 的 `create_ticket` 开始：先由装饰器检查登录、角色和 CSRF，再校验两个字段，最后把当前登录用户作为创建人写入数据库。SQL 参数通过占位符传入，不把输入拼进查询语句。

数据库连接的 `with` 块负责成功提交、失败回滚；请求结束时由 `db.py` 关闭连接。测试还会重新查数据库，确认返回结果与实际数据一致。

学习验收先解释三个问题：为什么创建人不从请求中读取，为什么登录后要换令牌，以及返回201之后还需要核对哪些数据库结果。独立修改校验规则与运行回归的练习尚待本人完成。
