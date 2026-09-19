# 安全编码基线

## 输入与查询

- 所有 SQL 必须参数化。禁止用 f-string、`%`、`+` 或 `.format()` 拼接查询语句。
- ORM 的 `raw()` / `extra()` 同样视为拼接，需走白名单校验。
- 排序字段、表名这类无法参数化的位置，必须用枚举白名单映射，不能直接取用户输入。

## 命令执行

- 禁止 `os.system`、`os.popen`；禁止 `subprocess` 传 `shell=True`。
- 外部命令用列表参数形式调用，并对参数做白名单校验。
- 需要调用系统工具时优先找 Python 库替代。

## 密钥与配置

- 密钥、口令、私钥一律来自环境变量或密钥管理服务，禁止硬编码。
- 仓库内只允许存在 `.env.example`，真实 `.env` 必须在 `.gitignore` 中。
- 一旦密钥进入 git 历史，视为已泄露：轮换密钥，不要只删文件。

## 传输与证书

- 禁止 `verify=False`、`InsecureSkipVerify` 等关闭证书校验的写法。
- 内网自签证书应把 CA 加入信任链，而不是关校验。
- 对外回调地址必须校验，避免 SSRF：解析后检查目标 IP 不在内网段。

## 反序列化与动态执行

- 禁止 `pickle.loads`、`marshal.loads` 处理外部数据。
- YAML 一律用 `yaml.safe_load`。
- 禁止 `eval` / `exec`；需要解析字面量用 `ast.literal_eval`。

## 认证与凭据存储

- 口令一律用 bcrypt / Argon2 加盐哈希，禁止 MD5、SHA1。
- JWT 必须校验签名，算法使用显式白名单，禁止接受 `none`。
- 日志与错误响应中禁止出现口令、token 原文。

## 上线配置

- 生产环境禁止开启调试模式（`debug=True`、`FLASK_DEBUG=1`）。
- CORS 禁止 `allow_origins=["*"]` 与 `credentials=True` 同时出现。
- 错误响应只返回通用提示，堆栈信息仅写日志。
