# 故障工单后端与自动化验证

基于 Flask、Python 原生 sqlite3 和 pytest 的个人项目，使用模拟故障和自编示例文档，实现工单业务、带来源的本地知识检索，并留下可重复执行的验证。AI 辅助开发，未使用企业代码或真实业务数据。

**当前已完成工单业务、可查询的状态历史和小型知识检索，另实现默认关闭的模型辅助回答接口。模型分支仅完成模拟测试，尚未调用真实外部模型。** 投递时可使用“故障工单后端与自动化验证”作为项目名称，并按实际验证范围描述知识能力。

## 当前功能

- 预置两名提单人和一名处理人；密码保存为哈希，登录使用 Flask 会话。
- 登录、退出、创建和状态更新操作验证 CSRF 令牌，登录成功后更换令牌。
- 提单人创建工单、查看自己的工单；处理人可以查看任意工单，但不能以处理人身份提单。
- 列表支持状态筛选与分页，列表内容和总数均按当前用户权限过滤。
- 处理人只能按 `pending → in_progress → resolved` 更新状态；解决时必须填写处理结果，解决人由服务端确定。
- 状态、结果和解决人通过同一条条件更新写入；重复、跳步、回退或竞争失败返回409，数据库失败时回滚。
- 状态历史与业务更新在同一事务中保存，包含操作人、前后状态、请求编号和时间；历史查询沿用工单可见范围，更新失败不留下成功历史。
- 创建人、初始状态和时间由服务端确定，拒绝请求中的额外字段。
- 校验字符串类型、空白、长度和编码；请求失败时返回统一 JSON 错误。
- 状态更新提交成功后记录工单、操作人和前后状态，不在这条日志中记录处理文本。
- 每个请求由服务端生成编号，通过响应头与请求完成、异常、状态更新日志关联；响应设置 `Cache-Control: no-store`。
- 使用独立临时数据库测试正常流程、越权、错误输入、CSRF、竞争更新及数据库写入失败恢复。
- 批量导入自编 UTF-8 Markdown，按原文行号分块，支持重复导入、修改后更新，以及批次失败回滚。
- 通过词法匹配检索相关片段，返回文档、行号和校验值；支持按工单标题与描述检索，资料不足时明确返回空结果。
- 处理人可请求辅助回答；默认关闭外部模型。有资料时可选择启用 DeepSeek，校验返回格式和引用，限制请求次数并保留预算记录；当前只验证了模拟响应与失败分支。

当前没有持续集成、公网部署或性能测试。知识模块是小语料的词法检索，不是语义检索或已经验证可靠的 RAG 系统；返回相关片段不代表它一定能回答问题。

## 本地启动

当前在 Windows、Python 3.13.9、Flask 3.1.2、SQLite 3.51.0 和 pytest 8.4.2 上验证。依赖清单见 [requirements.txt](requirements.txt) 和 [requirements-dev.txt](requirements-dev.txt)；本次使用已有环境，没有执行依赖安装。

先在项目根目录打开 PowerShell，并选择可用的 Python 解释器。本次验证使用下面的 Anaconda 安装路径；在其他电脑上，请将 `$python` 改成实际解释器路径，或在已有可用环境中设为 `'python'`。如果 `python` 打开 Microsoft Store，需直接指定解释器路径。

```powershell
$python = Join-Path $env:LOCALAPPDATA 'anaconda3\python.exe'
& $python --version
```

已有上述依赖时，可以直接执行后面的初始化。首次在新环境复现时，先用选定的解释器创建项目内虚拟环境并安装依赖，再继续；下面是复现说明，本次开发未执行环境创建或依赖安装：

```powershell
& $python -m venv .venv
if ($LASTEXITCODE -ne 0) { throw '虚拟环境创建失败，请检查选用的 Python。' }
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
& $python -m pip install -r requirements-dev.txt
if ($LASTEXITCODE -ne 0) { throw '依赖安装失败，请先检查错误信息。' }
```

直接使用虚拟环境中的解释器，无需运行激活脚本或更改执行策略。已完成依赖准备后，初始化并启动：

```powershell
$env:TICKET_SECRET_KEY = (& $python -c "import secrets; print(secrets.token_hex(32))").Trim()
$env:TICKET_DATABASE = Join-Path (Get-Location) 'instance\tickets.sqlite3'
& $python -m flask --app ticket_backend init-db
& $python -m flask --app ticket_backend seed-demo
& $python -m flask --app ticket_backend run --host 127.0.0.1 --port 5000
```

`seed-demo` 会要求输入并确认演示密码，长度为 8～256 个字符。它为 `submitter_a`、`submitter_b`、`handler` 三个模拟账号设置相同的演示密码；重复执行不会重置已有账号。初始化命令也不会删除已有工单。更新旧版代码后再次运行 `init-db` 可增加缺少的表，但不会编造此前未记录的状态历史。

保持这个终端运行，在另一终端按 [接口与演示](docs/first-step.md) 调用接口。当前没有网页首页。密钥只在当前终端环境中设置，不要提交到仓库；换密钥后需要重新登录。

## 本地知识检索与可选回答

在设置了相同解释器、应用密钥和数据库路径的终端中，导入仓库的6篇自编模拟资料：

```powershell
& $python -m flask --app ticket_backend import-knowledge examples/knowledge
```

重复导入相同内容不会重复建片段。目录中缺少旧文件不会自动删除已导入资料；文件数、大小、编码和分块均有限制，具体规则见[知识模块说明](docs/knowledge.md)。

登录后可使用以下接口；会话与手动调用方法见[接口与演示](docs/first-step.md)：

- `GET /tickets/<id>/history`：查询可见工单的状态历史。
- `GET /knowledge/search?q=SQLITE_BUSY&limit=3`：检索片段，返回来源和原文行号；`limit` 默认3，范围1～5。
- `GET /knowledge/documents/<id>`：读取参考文档全文，核对片段与校验值。
- `GET /tickets/<id>/knowledge`：用可见工单的标题和描述检索参考资料，最多使用前500个字符，并报告是否截断。
- `POST /tickets/<id>/answer`：处理人携带 CSRF 提交 `{"question":"Nginx 502 upstream Connection refused"}`，先检索再决定是否请求模型；不会自动修改工单或处理结果。

默认未启用模型：有资料时返回 `model_disabled` 和来源，无足够资料时返回 `insufficient_evidence`；两种情况均不调用外部服务。可选 DeepSeek `deepseek-flash` 接口已实现，配置和预算控制见[知识模块说明](docs/knowledge.md)。本轮已确认外部调用预算不超过10元，但尚未配置真实密钥，**实际外部模型调用0次、费用0元**。模拟测试通过不等于真实模型效果已验收。

## 一键演示完整流程

在项目根目录使用上述解释器即可运行，无需先启动服务或准备账号：

```powershell
& $python scripts/demo_workflow.py
```

脚本自动建立独立临时数据库和随机密码，在本机回环地址启动临时服务，通过真正的 HTTP 请求完成提单、隔离检查、状态更新、历史查询、资料检索与来源核对，并重建应用检查持久化。另验证模型关闭和无答案分支不发起外部调用。结束后关闭服务，仅清理本次新建的临时数据，不操作开发数据库。成功时输出 JSON，包含 `status: passed`、17组检查和42次 HTTP 请求；失败返回非零退出码。

固定知识问题的开发回归也可单独重复执行，无需准备开发库或模型密钥：

```powershell
$retrievalRunId = [guid]::NewGuid().ToString('N')
$retrievalReport = Join-Path (Get-Location) ('test-results\retrieval-' + $retrievalRunId + '.json')
& $python scripts/evaluate_knowledge.py --output $retrievalReport
```

本轮18个固定开发样例全部通过：12个有资料问题命中预期文档、6个无答案问题返回空结果，返回片段的12次来源核对通过。该固定集参与过开发调参，不是独立准确率评测，详见[验证记录](docs/verification.md)。

## 运行测试

在项目根目录使用同一个 `$python` 解释器。下面为每次运行生成新的临时目录，规避当前 Windows 默认 pytest 临时目录的权限问题，并保存 pytest 内置的 JUnit XML 结果，无需额外报告插件。

```powershell
New-Item -ItemType Directory -Force 'tests\.pytest-tmp' | Out-Null
$runId = [guid]::NewGuid().ToString('N')
$testTemp = Join-Path (Get-Location) ('tests\.pytest-tmp\run-' + $runId)
$testReport = Join-Path (Get-Location) ('test-results\pytest-' + $runId + '.xml')
& $python -m pytest -q --basetemp $testTemp --junitxml $testReport
```

测试不会使用开发数据库；测试配置缺少独立数据库路径时会拒绝启动。临时目录、数据库、测试结果、日志和本地配置均被 Git 忽略。pytest 会管理 `--basetemp` 指定的目录，请保留上述新建随机目录的方式。

2026年10月23日本轮完整回归为 **298 项测试通过**，其中49项为模型分支的模拟测试；另已跑通上述真实 HTTP 演示，并核对每个响应的请求编号和缓存头。环境、方法和范围见[验证记录](docs/verification.md)，测试设计依据见[测试指南](docs/testing-guide.md)。这些是功能与回归验证，不是性能测试或生产可用性结论。

## 阅读与后续顺序

1. [接口与演示](docs/first-step.md)：输入、返回、权限和完整工单流程。
2. [问题排查案例](docs/troubleshooting.md)：超长工单编号的定位、修复与回归。
3. [验证记录](docs/verification.md)：已执行检查和已知限制。
4. [投递与展示材料](docs/job-presentation.md)：同一项目的后端与测试开发展示方式。
5. [知识模块说明](docs/knowledge.md)：资料导入、检索、来源核对与可选模型配置。
6. [测试指南](docs/testing-guide.md)和[AI 辅助开发记录](docs/ai-development-record.md)：测试依据、实际检查和本人待完成的验收。

当前可以准备投递与演示练习。数据库继续使用 SQLite，没有实施或验证 MySQL；真实外部模型效果、独立留出问题集和本人独立修改验收仍待完成，不能写成已有成果。

AI 可以辅助实现，但学习验收还需要本人解释关键代码、独立完成小改动并验证结果。代码和自动化验证完成，不等于已经独立掌握相关能力。
