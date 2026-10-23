# 测试设计与缺陷回归方法

本项目使用模拟账号、故障和处理结果。测试由 AI 辅助设计、编写与执行；本文说明仓库中实际采用的方法，不表示学习者已经独立掌握。最新执行结果、环境和限制以[验证记录](verification.md)为准。

## 从需求确定风险和用例

先写业务规则和失败后的预期，再决定怎样发请求。例如，“只有处理人可以按待处理、处理中、已解决的顺序推进；解决时必须留下结果”。由这条规则得到以下风险：

- 提单人绕过权限改状态：使用自己的工单和他人的工单分别尝试，预期403且数据库不变。
- 处理人跳步、回退或重复提交：预期409，原状态、结果和解决人全部保留。
- 结果为空、类型错误或超长：预期400，不能保存一半的数据。
- 两个处理人读到同一旧状态：只允许一个成功，另一个返回冲突，最终结果属于成功的请求。
- SQL 执行或最终提交失败：返回通用500，整笔修改回滚，不能提前记录成功。

这些用例保存在 [test_ticket_workflow.py](../tests/test_ticket_workflow.py) 的 `test_submitters_cannot_change_even_owned_ticket`、`test_all_forbidden_status_transitions_leave_every_field_unchanged`、`test_invalid_status_payloads_cannot_write` 和 `test_two_handlers_cannot_overwrite_each_others_resolution`。它们检查业务后果，不以接口是否返回200作为唯一标准。

查询同样需要检查数据泄漏。[同一文件](../tests/test_ticket_workflow.py) 的 `test_lists_and_totals_apply_owner_and_status_filters` 同时核对列表条目和总数；如果只过滤条目却返回所有人的总数，这个用例也应失败。分页测试再检查顺序、边界和筛选后的总数。

输入按正常值、边界值和错误类型拆分。例如标题去空白后允许1至100个字符，除了测空和101个字符，还要确认1和100均能保存。对应 [test_tickets.py](../tests/test_tickets.py) 的 `test_invalid_fields_are_rejected_without_writes`、`test_inclusive_length_limits_accept_valid_strings` 和 Unicode 往返用例。参数化输入分别计为测试项，不代表新增了同等数量的功能。

## 数据准备和隔离

[conftest.py](../tests/conftest.py) 的 `app` fixture 为每个测试在 `tmp_path` 下创建独立 SQLite 文件，初始化表并准备两个提单人和一个处理人。客户端 fixture 实际获取 CSRF、登录并取得新令牌；不同用户使用独立 Cookie 会话。

测试不依赖开发库已有的工单，也不依赖其他测试先执行。已解决工单通过创建、推进、解决的业务请求准备；专门验证数据库故障时，才直接在该测试库中添加触发器或辅助表。夹具和临时目录负责隔离，不通过清空开发库来重置数据。

[test_config.py](../tests/test_config.py) 验证测试配置必须显式给出数据库路径，并拒绝默认开发数据库。运行方式见 [README](../README.md)：每次使用新的随机 `--basetemp` 目录，避免复用已有数据目录。

## 同时检查响应和数据库

每个重要写入场景分别核对两层结果：

1. 请求层：状态码、JSON 错误码或完整业务字段是否符合约定。
2. 持久化层：重新查询数据库，确认该写入确实保存，或被拒绝后整行保持原样。

例如 `test_complete_workflow_preserves_owner_and_records_handler` 核对状态变化、结果去空白、解决人来自当前账号，并检查创建人和创建时间保持不变。非法状态用例在请求前后比较整行，避免漏掉“返回409但结果已被覆盖”的错误。

身份和请求令牌要分别验证。[test_auth.py](../tests/test_auth.py) 的 `test_login_identifies_user_and_rotates_csrf_token` 确认登录后旧令牌被拒绝、新令牌可用；[test_tickets.py](../tests/test_tickets.py) 的 `test_creation_enforces_csrf_even_when_authenticated` 确认登录成功也不能省略 CSRF。

## 故障注入和竞争更新

[test_tickets.py](../tests/test_tickets.py) 的 `test_sqlite_write_failure_is_logged_hidden_and_leaves_no_ticket` 在测试库设置 `BEFORE INSERT` 触发器中止创建，检查通用500、没有新增工单、响应不暴露内部细节；移除触发器后，同一操作应成功。

[test_ticket_workflow.py](../tests/test_ticket_workflow.py) 的 `test_simulated_database_update_failure_rolls_back_and_recovers` 使用 `AFTER UPDATE` 触发器中止状态更新，比较失败前后的完整工单，再移除故障验证恢复。

[test_commit_failure.py](../tests/test_commit_failure.py) 的 `test_commit_failure_rolls_back_ticket_and_trigger_writes_then_recovers` 使用延迟外键约束，让 UPDATE 已执行但 COMMIT 失败。断言业务数据和触发器附带写入均回滚、没有成功状态日志，恢复后只记录一次成功更新。这与语句阶段失败是两个不同场景，均为人为模拟，未发生在生产系统。

竞争更新用例使用两个处理人的独立客户端和连接，通过线程屏障让双方都读到相同旧状态，再提交不同的处理结果。预期恰好一个200、一个409，数据库中的结果和解决人对应成功者。它验证指定竞争条件，不提供吞吐量或“高并发”结论。

## 复现、修复、回归与关闭

实际缺陷案例见[超长编号排查记录](troubleshooting.md)：初版整数路由在进入业务校验前就转换5000位编号，引发异常。修复改为先接收字符串、检查格式和范围，再转换。

保留的回归用例是 [test_tickets.py](../tests/test_tickets.py) 的 `test_ticket_path_beyond_python_integer_digit_limit_returns_404`。当前版本应统一返回404。关闭该缺陷需要同时满足：原异常输入被正确处理，正常编号和权限行为仍通过，相关边界回归以及完整回归通过；仅改代码或仅收到一次404都不够。执行结果见[验证记录](verification.md)。

提交缺陷记录时保存复现条件、预期与实际行为、定位依据、修复说明、用例名称和本次结果。接口500可用响应头 `X-Request-ID` 关联后台异常和完成日志；相关断言见 [test_observability.py](../tests/test_observability.py)。请求耗时用于排查，不充当性能报告。

## 演示和本轮扩展的边界

Flask 测试客户端用于快速回归；[demo_workflow.py](../scripts/demo_workflow.py) 另起本机临时服务，使用真实 HTTP 和独立会话验证流程与重建应用后的持久化。两者互补，不等同于浏览器端到端或公网部署验证。

本轮已把状态历史、小型知识检索、可选模型接口和本地配置／连接检查入口纳入完整回归，最新结果为334项通过。状态历史验证工单与历史同事务、失败不增加事件；知识模块验证导入幂等、更新引用失效与真实来源；模型模块只替换外部 HTTP，实际执行权限、检索、来源检查和持久预算，并验证提供方错误、HTTP 连接／读取中断与并发额度。

固定18题属于开发回归集，曾用于定位自然问法漏检并调整规范化，不能作为独立准确率。pytest 的模型响应全部模拟，不发真实请求；另通过 `scripts/check_model.py --live` 和 `--live --suite` 执行真实服务检查，累计4次调用，6题套件检查3个可答来源和3个无答案零调用。套件遇错误即停，正常3次、最坏最多6次外部尝试，复用原预算账本；需显式运行，重复执行可能计费。报告的语义审核仍待本人完成；详细方法、结果和限制见[验证记录](verification.md)。
