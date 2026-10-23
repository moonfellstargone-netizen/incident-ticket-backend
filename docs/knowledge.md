# 工单关联知识检索与可选模型回答

本版本已实现本地 Markdown 导入、词法检索、工单关联、原文来源核对，以及可选 DeepSeek 辅助回答接口。6篇资料由 AI 辅助撰写，均标注个人项目模拟场景，不含企业材料。模型默认关闭；本地密钥已保存但真实外部调用仍为0，模型分支只经过模拟响应验证，不据此声称完成可靠 RAG 验收。

## 导入和检索

按 [README](../README.md) 准备解释器、密钥环境变量和数据库。在已有开发库上先重新执行 `init-db`，只增加缺少的表，保留原工单；旧工单的历史不补造。

```powershell
& $python -m flask --app ticket_backend init-db
& $python -m flask --app ticket_backend import-knowledge examples/knowledge
```

重复导入相同字节标记 unchanged，不新增文档或分块。修改文件后再导入会更新原文、校验值和分块；旧 chunk_id 不复用。导入目录中缺失的文件不会自动删除旧资料，目前没有删除接口。

导入边界为顶层1～50篇 Markdown、每篇不超过128 KiB、每批不超过1 MiB；文件名为1～80字符的 ASCII 字母数字、下划线和连字符，首字符为字母或数字。文件必须严格 UTF-8，有 `# ` 标题，不包含 NUL，单行不超过1200字符；目录内的链接文件和子目录不作为资料导入。整个批次先校验，再事务写入，出错不保留半批数据。

登录后可调用：

- `GET /knowledge/search?q=SQLITE_BUSY&limit=3`：问题去空白后1～500字符，limit 默认3、范围1～5，拒绝未知与重复参数。
- `GET /knowledge/documents/{document_id}`：读取原文、标题、slug、更新时间和 SHA256 校验值。当前资料是供全部登录账号共享的模拟知识，不提供私有文档权限。
- `GET /tickets/{ticket_id}/knowledge`：使用有权查看的工单标题和描述进行检索；总共最多取前500字符，通过 `query_truncated` 告知是否截断，不接受额外查询参数。不可见工单返回404。

搜索结果位于 `data.query`、`data.status`、`data.items`。每条包含 chunk_id、document_id、slug、title、excerpt、start_line、end_line、checksum 和 score。行号从1开始，片段可按原文行范围核对；更新期间旧分块会失效，校验值用于区分版本。

当前采用小语料词法基线：英文词和错误码、中文相邻双字词，经通用疑问词规范化后做 BM25 风格排序，并设置有效词覆盖与错误码精确匹配规则。score 是相关性排序值，不是回答正确的概率。匹配不足时返回 `insufficient_evidence` 和空列表；同义词、长描述和混合主题仍可能漏检或误匹配，没有向量检索或语义检索能力。

## 固定问题与来源验证

```powershell
$retrievalReport = Join-Path (Get-Location) ('test-results\retrieval-' + [guid]::NewGuid().ToString('N') + '.json')
& $python scripts/evaluate_knowledge.py --output $retrievalReport
```

脚本使用独立临时 SQLite，导入6篇资料，执行 [18个固定问题](../examples/knowledge_questions.json)，检查目标资料是否进入前三项、无答案问题是否为空、片段行号和校验值是否对应原文。不会启动外部模型或操作开发库。

本轮最终12个可答样例全部命中首项，6个无答案样例全部为空，12次来源核对全部通过。首次运行有一题自然问法漏检，随后补充通用疑问词规范化并加入不同问法和未知主题回归。该集合已经参与开发调整，属于开发回归集，不是独立测试集，不代表任意问题的准确率。真实模型回答的语义正确性尚未评测。

## 辅助回答接口

`POST /tickets/{ticket_id}/answer` 仅处理人可用，需登录会话和 `X-CSRF-Token`，JSON 只接受 `question`，去空白后1～500字符。该操作不更改工单状态或处理结果，建议由处理人核对资料后另行填写最终结果。

```json
{"question":"SQLITE_BUSY database is locked"}
```

资料不足时返回200、`status: insufficient_evidence`、空 answer/sources、`model_called: false`，不会调用模型。模型关闭时返回200、`status: model_disabled`、`answer: null` 及真实检索来源，不用模板伪装生成回答。模型启用并成功时返回回答、服务端来源、模型名和 token 用量；模型也可以明确拒答。

调用提供方时仅发送问题与最多3个检索片段，不发送账号、Cookie、密钥以外的认证信息或整库资料。提示词把问题和来源作为数据，不提供工具执行能力。服务端要求结构化回答，拒绝空引用、重复引用、未提供的分块编号、截断或错误格式；返回前再次核对分块和校验值，资料在请求期间更新则409 `KNOWLEDGE_CHANGED`，弃用旧回答。编号存在只能证明引用来自本轮资料，不能证明回答每句话都有依据，仍需人工核对。

缺少密钥或预算配置错误返回503；预算耗尽429；上游不可用503；上游其他错误或回答格式/引用错误502；连接失败或 socket 超时504。超时不自动重试。错误响应不包含密钥、提供方原始错误正文或问题全文；调用方可继续使用本地检索接口查原文。

## 模型配置与本轮预算

用户已确认 DeepSeek 官方 `deepseek-flash` 和本轮10元人民币上限。本地密钥已保存并通过格式及应用读取检查，提供方是否接受仍待实际请求确认。本轮只运行本地与模拟验证，实际费用为0。

准备真实验证时，可把密钥单独一行保存到项目根目录的 `deepseek_api.txt`（UTF-8），再从文件导入，避免终端隐藏输入时无法确认是否收到内容：

```powershell
& $python scripts/configure_model.py --from-file deepseek_api.txt
```

文件中仅放密钥，不加引号、命令或说明；脚本兼容 UTF-8 BOM 和末尾换行，保留原文件。`deepseek_api.txt` 和目标目录 `instance/` 均被 Git 忽略。也可使用隐藏输入：

```powershell
& $python scripts/configure_model.py
```

脚本只报告收到的字符数及错误类别，不回显密钥；终端无法安全隐藏输入时要求改用文件，不退回明文输入。空内容、内部空白、多行、非 ASCII 字符和过长输入会给出具体原因，不依赖未经确认的固定长度或前缀规则。

它只向被 Git 忽略的 `instance/deepseek_api_key.txt` 保存密钥，不覆盖已存在文件；更新时显式加 `--replace`，替换失败保留旧配置。保存不启用模型、不发起调用。也可由本地环境变量 `DEEPSEEK_API_KEY` 提供，环境变量优先；不要把密钥写入启动命令参数或提交仓库。本地格式通过不代表提供方认可密钥。

首次接通可直接运行单次检查，不需要先初始化开发库、启动 Flask 或手动登录：

```powershell
& $python scripts/check_model.py --live
```

`--live` 显式允许一次可能计费的请求。脚本准备自己的临时业务数据库、模拟账号和资料，经 Flask 测试客户端调用实际业务接口，再由后端向 DeepSeek 发起真实 HTTP；最多请求一次，不自动重试，沿用原持久预算账本。它不会使用或重置开发工单库，也不会因为重建临时库重新获得20次额度。

结果保存到被 Git 忽略的 `test-results/live-model-*.json`。`status: passed` 表示本次得到带有效来源的回答，并核对了原文；回答含义仍需人工审核。`needs_review` 表示未得到可采用的回答；失败时查看 `error.code`，不要只看 HTTP 状态。未加 `--live` 会直接退出，不发请求。当前脚本仅通过6项模拟传输测试，尚未执行真实付费验证。

若出现 `MODEL_NOT_CONFIGURED`，检查是否保存了密钥，或是否存在优先级更高的空／旧 `DEEPSEEK_API_KEY` 环境变量；`MODEL_UPSTREAM_ERROR` 需要到官方平台核对密钥和 API 账户余额，当前接口不细分提供方401和402。`MODEL_TIMEOUT` 时先排查连接，不无限重试；`MODEL_BUDGET_EXHAUSTED` 时保留账本，核对已用额度。

确认准备好真实验证后，在按 README 配置好 Flask 的同一终端显式开启：

```powershell
$env:TICKET_ENABLE_LLM = '1'
$env:TICKET_MODEL_BUDGET_CNY = '10'
& $python -m flask --app ticket_backend run --host 127.0.0.1 --port 5000
```

本实现固定使用[官方 Chat Completions 接口](https://api-docs.deepseek.com/api/create-chat-completion/)，关闭思考，最多输出600 tokens，发送请求体最多32000字节，响应读取上限64 KiB，socket 超时10秒。当前验收没有执行上述开启后的真实调用。

应用使用独立持久账本 `instance/model-usage.sqlite3`：每次网络请求前通过短事务预占0.5元，累计预算最多10元、最多20次；失败、超时或弃用旧回答也不释放预占，避免未知扣费导致自动重试。重启应用或重建业务库不会重置这份默认账本。测试用独立临时账本，默认禁用模型，即使开发环境已开启也不会误发请求。

按2026年10月23日核对的[官方人民币价格](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/)，Flash 高峰缓存未命中输入为每百万 tokens 2元、输出8元。账本依此记录保守估算，不等于实际账单；缓存、时段和后续价格变化会影响费用。应用侧限额不能控制同一账户在其他程序的消费，实际费用以提供方账单为准。
