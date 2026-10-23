# 知识检索与模型配置

使用6篇模拟故障 Markdown 文档，先检索相关片段，再按需生成带来源的辅助回答。

## 导入与接口

按 [README](../README.md) 设置解释器、应用密钥和数据库后导入：

```powershell
& $python -m flask --app ticket_backend import-knowledge examples/knowledge
```

相同内容重复导入不新增，修改后重新导入会更新片段并使旧引用失效；目录缺少文件不会自动删除旧资料。每批1～50篇、每篇不超过128 KiB、合计不超过1 MiB，要求 UTF-8 和 `# ` 标题；校验失败整批不写入。

登录后可调用：

- `GET /knowledge/search?q=SQLITE_BUSY&limit=3`：查询1～500字符，limit 为1～5，返回片段、文档、行号和校验值。
- `GET /knowledge/documents/{id}`：读取原文。
- `GET /tickets/{id}/knowledge`：按有权查看的工单标题和描述检索，最多使用前500字符。
- `POST /tickets/{id}/answer`：仅处理人可用，携带会话和 CSRF，提交 `{"question":"SQLITE_BUSY database is locked"}`。

检索采用英文词、错误码和中文双字词的词法排序。资料不足返回 `insufficient_evidence`；有资料但模型关闭时返回 `model_disabled` 和来源。辅助回答不修改工单，模型引用仅限本次检索片段，来源在生成期间更新则返回409 `KNOWLEDGE_CHANGED`。

## 配置模型

外部模型默认关闭，使用 DeepSeek 官方 `deepseek-flash`。把密钥单独一行保存到项目根目录的 UTF-8 文件 `deepseek_api.txt`，执行：

```powershell
& $python scripts/configure_model.py --from-file deepseek_api.txt
```

密钥保存到 `instance/deepseek_api_key.txt`，两处文件均被 Git 忽略；更新时显式加 `--replace`。保存配置不调用模型。也支持环境变量 `DEEPSEEK_API_KEY`，优先于文件。

检查真实连接，可能计费，无需先启动 Flask：

```powershell
& $python scripts/check_model.py --live
```

默认最多调用一次。可用 `--live --suite` 检查6个固定开发样例：正常调用模型3次，另外3个无答案问题应不调用；最坏最多6次尝试，遇首个不通过即停，不重试。报告保存到 `test-results/live-model-*.json`；`passed` 表示业务与来源检查通过，回答内容仍需核对。

在开发服务中启用模型，按 README 准备好应用配置后执行：

```powershell
$env:TICKET_ENABLE_LLM = '1'
$env:TICKET_MODEL_BUDGET_CNY = '10'
& $python -m flask --app ticket_backend run --host 127.0.0.1 --port 5000
```

仅发送问题与最多3个片段；10秒 socket 超时、输出上限600 tokens，不自动重试。持久账本为 `instance/model-usage.sqlite3`，默认预算10元、最多20次尝试，每次预占0.5元，失败也保留预占。重启或重建业务库不重置额度；预占与估算均不等于实际账单。

常见错误：`MODEL_NOT_CONFIGURED` 检查密钥或环境变量；`MODEL_AUTH_FAILED` 检查平台认证；`MODEL_BALANCE_INSUFFICIENT` 核对余额；`MODEL_TIMEOUT` 检查连接；`MODEL_BUDGET_EXHAUSTED` 核对账本，保留已有记录。其他错误码见[接口说明](first-step.md)。

## 本地检索回归

```powershell
$retrievalReport = Join-Path (Get-Location) ('test-results\retrieval-' + [guid]::NewGuid().ToString('N') + '.json')
& $python scripts/evaluate_knowledge.py --output $retrievalReport
```

脚本使用临时库，不调用模型。最近验证见[验证记录](verification.md)。固定问题属于开发回归集，引用可核对不代表任意回答都正确。
