# 故障工单后端与自动化验证

基于 Flask、Python 原生 sqlite3 和 pytest 的个人项目，使用手工录入的模拟故障，实现工单业务并留下可重复执行的验证。AI 辅助开发，未使用企业代码或真实业务数据。

**当前已完成工单业务最小版本：登录、创建、列表与详情查询、单向状态流转和处理结果，以及自动化回归和真实 HTTP 演示。知识检索尚未实现。** 投递时可使用“故障工单后端与自动化验证”作为当前项目名称。

## 当前功能

- 预置两名提单人和一名处理人；密码保存为哈希，登录使用 Flask 会话。
- 登录、退出、创建和状态更新操作验证 CSRF 令牌，登录成功后更换令牌。
- 提单人创建工单、查看自己的工单；处理人可以查看任意工单，但不能以处理人身份提单。
- 列表支持状态筛选与分页，列表内容和总数均按当前用户权限过滤。
- 处理人只能按 `pending → in_progress → resolved` 更新状态；解决时必须填写处理结果，解决人由服务端确定。
- 状态、结果和解决人通过同一条条件更新写入；重复、跳步、回退或竞争失败返回409，数据库失败时回滚。
- 创建人、初始状态和时间由服务端确定，拒绝请求中的额外字段。
- 校验字符串类型、空白、长度和编码；请求失败时返回统一 JSON 错误。
- 状态更新提交成功后记录工单、操作人和前后状态，不在这条日志中记录处理文本。
- 使用独立临时数据库测试正常流程、越权、错误输入、CSRF、竞争更新及数据库写入失败恢复。

完整状态审计历史、知识检索、模型接入、持续集成和公网部署暂不做。当前状态日志不等于可查询的审计历史。

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

## 一键演示完整流程

在项目根目录使用上述解释器即可运行，无需先启动服务或准备账号：

```powershell
& $python scripts/demo_workflow.py
```

脚本自动建立独立临时数据库和随机密码，在本机回环地址启动临时服务，通过真正的 HTTP 请求完成提单、隔离检查、状态更新、结果读取和应用重新启动后的持久化检查。结束后关闭服务，仅清理本次新建的临时数据，不操作开发数据库。成功时输出 JSON，包含 `status: passed`、13组检查和32次 HTTP 请求；失败返回非零退出码。

## 运行测试

在项目根目录使用同一个 `$python` 解释器。下面为每次运行生成新的临时目录，规避当前 Windows 默认 pytest 临时目录的权限问题。

```powershell
New-Item -ItemType Directory -Force 'tests\.pytest-tmp' | Out-Null
$testTemp = Join-Path (Get-Location) ('tests\.pytest-tmp\run-' + [guid]::NewGuid().ToString('N'))
& $python -m pytest -q --basetemp $testTemp
```

测试不会使用开发数据库；测试配置缺少独立数据库路径时会拒绝启动。临时目录、数据库、日志和本地配置均被 Git 忽略。pytest 会管理 `--basetemp` 指定的目录，请保留上述新建随机目录的方式。

2026年10月23日的验证结果为 **152 项测试通过**，另已跑通上述真实 HTTP 演示。环境、方法和范围见 [验证记录](docs/verification.md)。这些是功能与回归验证，不是性能测试或生产可用性结论。

## 阅读与后续顺序

1. [接口与演示](docs/first-step.md)：输入、返回、权限和完整工单流程。
2. [问题排查案例](docs/troubleshooting.md)：超长工单编号的定位、修复与回归。
3. [验证记录](docs/verification.md)：已执行检查和已知限制。
4. [投递与展示材料](docs/job-presentation.md)：同一项目的后端与测试开发展示方式。

当前先完成投递和演示练习。知识检索属于后续待确认的扩展，需要自编资料、来源引用、无答案处理和固定问题样例的实际验证；不能仅凭项目原名称声称已经完成。

AI 可以辅助实现，但学习验收还需要本人解释关键代码、独立完成小改动并验证结果。代码和自动化验证完成，不等于已经独立掌握相关能力。
