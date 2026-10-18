# 超长工单编号的排查与回归

本案例来自本项目开发时发现的输入处理缺陷。使用模拟请求在本地验证，未发生在生产系统中。

## 现象与复现

使用正常提单账号登录后，请求 `/tickets/` 后接5000位数字。预期是统一的404错误，实际初版在进入视图函数前就抛出了 `ValueError`，无法按约定返回错误。

正常工单编号可以查询，因此排查先比较正常编号与超长编号的请求路径，而不是修改数据库或关闭参数限制。

## 原因

初版使用 Flask 的整数路由转换器。Werkzeug 在路由匹配阶段先把路径转换为 Python 整数，超过当前解释器的整数转换位数限制后抛出异常。视图内即便增加 SQLite 整数范围检查，也来不及处理这个异常。

## 修复

查询路由先接收字符串，再检查编号长度、ASCII 数字格式和正整数64位范围，检查通过后才转换为整数并查询数据库。超出范围的编号统一按不存在处理。

修复没有扩大 Python 的整数转换上限，也没有把输入强行截断。

## 回归证据

`tests/test_tickets.py` 中的 `test_ticket_path_beyond_python_integer_digit_limit_returns_404` 专门保留5000位数字路径；其他用例覆盖正常工单读取、不同用户访问隔离，以及 SQLite 边界附近的编号。

按 README 设置 `$python` 和新的 `$testTemp` 后，可以只运行相关编号用例：

```powershell
& $python -m pytest -q tests/test_tickets.py -k 'ids_return_404 or integer_digit_limit' --basetemp $testTemp
```

最终全套测试通过。另有 SQLite 触发器故障注入用例，用于验证写入失败、错误隐藏、零新增和恢复后的成功写入；那一项是明确的模拟故障演练，不是生产事故记录。

## 排查中的另一个发现

真实启动验证还发现初始化命令缺少 Flask 应用上下文，会报 `Working outside of application context`。已给命令补上 `with_appcontext`，并增加 `tests/test_cli.py`，通过实际命令入口验证初始化与演示账号准备，避免仅测内部函数而漏掉启动步骤。
