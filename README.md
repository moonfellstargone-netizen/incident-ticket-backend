# 故障工单与知识检索后端系统

基于 Flask、Python 原生 sqlite3 和 pytest 的个人学习项目，使用手工录入的模拟故障，逐步实现工单业务、自动化验证和小型知识检索。

**当前完成第一个开发小步：预置账号登录、创建工单、查询详情及对应验证。第一阶段的工单状态流转尚未完成，知识检索尚未实现。**

## 当前功能

- 预置两名提单人和一名处理人；密码保存为哈希，登录使用 Flask 会话。
- 登录、退出和创建操作验证 CSRF 令牌，登录成功后更换令牌。
- 提单人创建工单、查看自己的工单；处理人可以查看任意工单，但不能以处理人身份提单。
- 创建人、初始状态和时间由服务端确定，拒绝请求中的额外字段。
- 校验字符串类型、空白、长度和编码；请求失败时返回统一 JSON 错误。
- 使用独立临时数据库测试正常流程、越权、错误输入、CSRF 和数据库写入失败。

工单列表、状态更新、处理结果接口、完整状态历史、知识检索和持续集成均未实现。数据库中的状态与最终结果约束为已确认的后续工单流程预留，不代表相应接口已经完成。

## 本地启动

当前在 Windows、Python 3.13.9、Flask 3.1.2、SQLite 3.51.0 和 pytest 8.4.2 上验证。依赖清单见 [requirements.txt](requirements.txt) 和 [requirements-dev.txt](requirements-dev.txt)；本次使用已有环境，没有执行依赖安装。

先在项目根目录打开 PowerShell。下面的解释器位置适用于本次验证使用的 Anaconda 安装；如果位置不同，只调整 `$python`，或在已激活的环境中设为 `'python'`。

```powershell
$python = Join-Path $env:LOCALAPPDATA 'anaconda3\python.exe'
& $python --version
$env:TICKET_SECRET_KEY = (& $python -c "import secrets; print(secrets.token_hex(32))").Trim()
$env:TICKET_DATABASE = Join-Path (Get-Location) 'instance\tickets.sqlite3'
& $python -m flask --app ticket_backend init-db
& $python -m flask --app ticket_backend seed-demo
& $python -m flask --app ticket_backend run --host 127.0.0.1 --port 5000
```

`seed-demo` 会要求输入并确认演示密码，长度为 8～256 个字符。它为 `submitter_a`、`submitter_b`、`handler` 三个模拟账号设置相同的演示密码；重复执行不会重置已有账号。初始化命令也不会删除已有工单。

保持这个终端运行，在另一终端按 [接口与演示](docs/first-step.md) 调用接口。当前没有网页首页。密钥只在当前终端环境中设置，不要提交到仓库；换密钥后需要重新登录。

## 运行测试

在项目根目录使用同一个 `$python` 解释器。下面为每次运行生成新的临时目录，规避当前 Windows 默认 pytest 临时目录的权限问题。

```powershell
New-Item -ItemType Directory -Force 'tests\.pytest-tmp' | Out-Null
$testTemp = Join-Path (Get-Location) ('tests\.pytest-tmp\run-' + [guid]::NewGuid().ToString('N'))
& $python -m pytest -q --basetemp $testTemp
```

测试不会使用开发数据库；测试配置缺少独立数据库路径时会拒绝启动。临时目录、数据库、日志和本地配置均被 Git 忽略。pytest 会管理 `--basetemp` 指定的目录，请保留上述新建随机目录的方式。

当前验证结果为 **73 项测试通过**，另已执行真实 HTTP 流程与重新打开数据库后的持久化检查。环境、方法和范围见 [验证记录](docs/verification.md)。这不是性能测试或生产可用性结论。

## 阅读与后续顺序

1. [接口与演示](docs/first-step.md)：输入、返回、权限和一次实际请求流程。
2. [问题排查案例](docs/troubleshooting.md)：超长工单编号的定位、修复与回归。
3. [验证记录](docs/verification.md)：已执行检查和已知限制。

下一小步按既定范围补工单列表、单向状态流转和最终处理结果；之后再完善自动化验证，基础稳定后评估持续集成。知识检索阶段使用自编资料，验证来源引用、无答案处理和固定问题样例。

AI 可以辅助实现，但学习验收还需要本人解释关键代码、独立完成小改动并验证结果。代码和自动化验证完成，不等于已经独立掌握相关能力。
