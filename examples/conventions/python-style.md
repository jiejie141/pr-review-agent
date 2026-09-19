# 后端代码规范 · Python

## 命名

- 模块与包用小写下划线：`user_service.py`，禁止 `UserService.py`。
- 类用大驼峰，函数与变量用小写下划线。
- 常量全大写并集中在模块顶部。
- 布尔变量以 `is_` / `has_` / `can_` 开头，避免 `flag`、`done` 这类无信息量的名字。
- 禁止单字母变量名，循环索引 `i` / `j` 除外。

## 异常处理

- 禁止裸 `except:`，禁止 `except Exception: pass`。
- 捕获后必须做至少一件事：记录日志、转换成领域异常、或明确降级。
- 确实需要忽略的异常，必须写明原因：`except KeyError:  # 字段可选，缺失属正常`
- 自定义异常统一继承 `AppError`，便于上层按类型分流。

## 日志

- 统一使用 `logging.getLogger(__name__)`，禁止用 `print` 输出运行信息。
- 日志内容禁止包含口令、token、身份证号等敏感字段；必要时脱敏为 `abc***xyz`。
- 异常日志用 `logger.exception(...)` 保留堆栈，不要 `logger.error(str(e))`。

## 资源管理

- 文件、连接、锁一律用 `with` / `try-finally` 管理，禁止裸 `open()` 后手动 `close()`。
- 数据库连接从连接池获取，禁止在循环内创建连接。

## 类型与校验

- 函数签名标注类型；公共函数必须有 docstring。
- 对外部输入（HTTP 参数、文件、环境变量）必须显式校验，不允许依赖 `assert`。
- 判空用 `is None` / `is not None`，不要用 `== None`。
- 可变对象（list / dict / set）不得作为函数默认参数。

## 提交与注释

- 不允许在主干留下无跟踪的 `TODO` / `FIXME`；有需要就开 issue 并在注释里引用编号。
- 注释解释「为什么」，不复述「做了什么」。
