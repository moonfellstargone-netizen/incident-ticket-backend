# 故障工单与知识检索系统

基于 Python、Flask、SQLite 和 pytest 的个人后端项目，使用模拟工单和自编故障文档，覆盖工单处理、接口测试与知识检索。

[GitHub 仓库](https://github.com/moonfellstargone-netizen/incident-ticket-backend) · [使用说明](使用说明.txt)

使用压缩包时，先完整解压到本地目录，再按使用说明准备环境和运行。

## 功能

- 工单创建、分页查询、状态筛选、处理结果与状态历史。
- 提单人与处理人权限隔离，会话登录和 CSRF 校验。
- 状态按 `pending → in_progress → resolved` 推进，条件更新防止覆盖，失败时事务回滚。
- Markdown 文档导入、工单关联检索与带来源的 DeepSeek 辅助回答；无资料时提示无法回答。

## 运行

在项目根目录打开 PowerShell。已验证环境：Windows、Python 3.13.9、Flask 3.1.2、SQLite 3.51.0。

首次运行，创建虚拟环境并安装依赖。没有 `py` 命令时，可用本机 Python 解释器创建虚拟环境，具体方法见[使用说明](使用说明.txt)。

```powershell
py -3 -m venv .venv
$python = Join-Path (Get-Location) '.venv\Scripts\python.exe'
& $python -m pip install -r requirements-dev.txt
```

初始化并启动：

```powershell
$env:TICKET_SECRET_KEY = (& $python -c "import secrets; print(secrets.token_hex(32))").Trim()
$env:TICKET_DATABASE = Join-Path (Get-Location) 'instance\tickets.sqlite3'
& $python -m flask --app ticket_backend init-db
& $python -m flask --app ticket_backend seed-demo
& $python -m flask --app ticket_backend import-knowledge examples/knowledge
& $python -m flask --app ticket_backend run --host 127.0.0.1 --port 5000
```

`seed-demo` 提示设置演示密码，账号为 `submitter_a`、`submitter_b`、`handler`；重复执行保留已有账号与工单。服务地址为 `http://127.0.0.1:5000`，通过接口调用，无网页首页。

## 演示与测试

完整流程演示自动准备临时数据库和账号，无需提前启动服务，不调用外部模型：

```powershell
& $python scripts/demo_workflow.py
```

运行自动化测试，使用独立的临时目录：

```powershell
& $python -m pytest -q --basetemp ("tests/.pytest-tmp/run-" + [guid]::NewGuid().ToString('N'))
```

最近验证（2026-10-23）：334项测试通过；HTTP 演示42次请求、17组检查通过；检索18题通过；模型接口6个固定样例通过。结果与方法见[验证记录](docs/verification.md)。

## 文档

- [接口与手动演示](docs/first-step.md)
- [知识检索与模型配置](docs/knowledge.md)：外部模型默认关闭，显式开启后可能计费。
- [测试设计](docs/testing-guide.md)与[问题排查](docs/troubleshooting.md)
- [简历描述与演示顺序](docs/job-presentation.md)

项目用于本机演示，检索采用词法匹配。固定样例用于回归验证，密钥、数据库和本地报告不包含在仓库与压缩包中。
