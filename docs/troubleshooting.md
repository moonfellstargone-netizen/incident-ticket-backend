# 问题排查记录

以下问题来自本项目本地开发或故障注入，使用模拟数据。

## 超长工单编号

**现象：** 请求 `/tickets/` 后接5000位数字，初版抛出 `ValueError`，未返回约定的404。

**原因：** Flask 整数路由在进入视图前先转换编号，超过 Python 的整数转换位数限制，视图内的校验来不及执行。

**修复：** 路由接收字符串，先校验长度、ASCII 数字和正整数64位范围，再转换和查库；无效编号返回404。

**回归：** [test_tickets.py](../tests/test_tickets.py) 中的 `test_ticket_path_beyond_python_integer_digit_limit_returns_404`，并覆盖正常编号、边界与访问权限。

## 自然问法漏检

**现象：** 18题开发回归首次17题通过，“模型调用连接超时，还能查看哪些本地资料”未命中 `model-timeout`。

**原因：** 问句产生过多查询意图词，文档有效词覆盖率低于0.5门槛。

**修复：** 增加通用疑问词规范化，保留阈值，并加入不同表达及未知主题回归。

**回归：** [test_knowledge.py](../tests/test_knowledge.py) 和 `scripts/evaluate_knowledge.py`，18题通过。该题集参与过调整，用作开发回归。

## 模型 HTTP 中断

**现象：** 模拟连接阶段 `BadStatusLine` 或读取阶段 `IncompleteRead` 时，初版返回500，调用账本停在 reserved。

**原因：** 原捕获范围为 `TimeoutError`、`URLError`、`OSError`，遗漏 `http.client.HTTPException`。

**修复：** 在外部调用边界捕获该异常，返回502 `MODEL_UPSTREAM_ERROR`，将账本记为 failed，保留预算预占且不重试。

**回归：** [test_model_answer.py](../tests/test_model_answer.py) 分别注入打开连接和读取中断，检查错误码、失败记录、只调用一次，以及响应和日志不含异常携带的敏感内容。

接口问题可用响应头 `X-Request-ID` 关联后台日志。完整测试结果见[验证记录](verification.md)。
