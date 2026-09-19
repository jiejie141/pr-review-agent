"""缺陷模式库。

规则引擎负责「确定性、可复现、零成本」的那部分判断：正则能抓住的、有明确
判据的缺陷。模型负责「需要理解语义」的那部分。

这么分工的原因很实际：
- 规则命中可以直接给出证据（原文片段）和行号，误报率低、可写进测试；
- 规则抓不住的跨文件语义问题交给模型，但模型输出必须通过行号校验才会被采纳。

如果全交给模型，成本高且不可复现；如果全交给规则，遇到「这个函数为什么慢」就瞎了。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .models import Category, Severity

_PY = (".py",)
_JS = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
_JVM = (".java", ".kt", ".scala")
_SQLISH = (".py", ".java", ".js", ".ts", ".go", ".php", ".rb", ".sql", ".kt")


@dataclass(frozen=True)
class Pattern:
    """一条缺陷模式。

    regexes: 命中任意一条即算命中，允许多种写法覆盖同一类缺陷。
    needs_context: 为 True 时，引擎把命中行前后的上下文一起送进来匹配
                   （用于「循环体内做昂贵操作」这类单行看不出来的问题）。
    """

    id: str
    title: str
    category: Category
    severity: Severity
    regexes: tuple[str, ...]
    suggestion: str
    extensions: tuple[str, ...] = ()
    needs_context: bool = False
    confidence: float = 0.8
    description: str = ""

    # 注释里的代码片段不应触发规则。典型例子：安全规范文档里写
    # `# 禁止使用 eval()` 或 `# 不要写 verify=False`，这些是「反例说明」
    # 而不是缺陷本身。CONV304（检查遗留 TODO）需要显式关掉这个行为。
    ignore_comments: bool = True
    _compiled: tuple[re.Pattern, ...] = field(default=(), compare=False, repr=False)

    def compiled(self) -> tuple[re.Pattern, ...]:
        if not self._compiled:
            object.__setattr__(
                self,
                "_compiled",
                tuple(re.compile(r, re.IGNORECASE if self.id.startswith(("SEC", "PERF")) else 0) for r in self.regexes),
            )
        return self._compiled

    def applies_to(self, path: str) -> bool:
        if not self.extensions:
            return True
        low = path.lower()
        return any(low.endswith(ext) for ext in self.extensions)

    def match(self, text: str) -> re.Match | None:
        for rx in self.compiled():
            m = rx.search(text)
            if m:
                return m
        return None


# --------------------------------------------------------------------------
# 安全维度：14 类
# --------------------------------------------------------------------------
_SECURITY: tuple[Pattern, ...] = (
    Pattern(
        id="SEC101",
        title="SQL 注入：动态拼接查询语句",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"\.(?:execute|executemany|raw|query)\s*\(\s*f[\"']",
            r"\.(?:execute|executemany|raw|query)\s*\(\s*[\"'][^\"']*[\"']\s*%",
            r"\.(?:execute|executemany|raw|query)\s*\(\s*[\"'][^\"']*[\"']\s*\+",
            r"\.(?:execute|executemany|raw|query)\s*\(\s*[\"'][^\"']*[\"']\s*\.format\s*\(",
            # 最常见的 Python 注入写法：SQL 里嵌引号再接 + 拼接。
            # 形如 execute(内层带引号的 SQL + 变量)，被内层引号截断后前几条规则都抓不到。
            r"\.(?:execute|executemany|raw|query)\s*\([^)]*[\"']\s*\+\s*[A-Za-z_]",
            r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b[^\"';]*[\"']\s*\+\s*[A-Za-z_]",
        ),
        suggestion="改用参数化查询（占位符 + 参数元组），让驱动负责转义，不要用字符串拼 SQL。",
        extensions=_SQLISH,
        confidence=0.85,
        description="把用户输入拼进 SQL 文本，攻击者可构造 `' OR 1=1 --` 绕过条件。",
    ),
    Pattern(
        id="SEC102",
        title="命令注入：以 shell 方式执行外部命令",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"subprocess\.(?:run|call|Popen|check_output|check_call)\s*\([^)]*shell\s*=\s*True",
            r"os\.(?:system|popen)\s*\(",
            r"commands\.getoutput\s*\(",
            r"(?<![\w.])exec\.(?:Command|CommandLine)\s*\(",
        ),
        suggestion="用列表参数形式调用（shell=False），让参数与命令分离；必须拼接时用 shlex.quote 并做白名单校验。",
        confidence=0.85,
        description="shell=True 会把参数交给 shell 解释，`;` `|` `$()` 都能逃逸出原命令。",
    ),
    Pattern(
        id="SEC103",
        title="硬编码凭据或密钥",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"(?:api[_-]?key|secret[_-]?key|access[_-]?key|password|passwd|token|private[_-]?key)\s*[:=]\s*[\"'][^\"'{}\s]{12,}[\"']",
            r"AKIA[0-9A-Z]{16}",
            r"gh[pousr]_[A-Za-z0-9]{30,}",
            r"sk-[A-Za-z0-9]{20,}",
            r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----",
        ),
        suggestion="移到环境变量或密钥管理服务，代码仓库里只保留 .env.example 占位；已提交的密钥要立刻轮换。",
        confidence=0.9,
        description="凭据进版本库等于对全部有仓库读权限的人公开，且会永久留在 git 历史里。",
    ),
    Pattern(
        id="SEC104",
        title="动态执行代码（eval / exec）",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"(?<![\w.])(?:eval|exec)\s*\(\s*(?![\"']\s*[\"'])",
            r"new\s+Function\s*\(",
            r"setTimeout\s*\(\s*[\"']",
        ),
        suggestion="用显式映射表代替动态求值；配置解析用 ast.literal_eval 或 json.loads。",
        confidence=0.75,
        description="eval 执行任意表达式，一旦输入可控即为远程代码执行。",
    ),
    Pattern(
        id="SEC105",
        title="不安全反序列化",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"pickle\.(?:load|loads)\s*\(",
            r"cPickle\.(?:load|loads)\s*\(",
            r"marshal\.loads\s*\(",
            r"yaml\.load\s*\((?![^)]*Loader\s*=\s*(?:yaml\.)?SafeLoader)(?![^)]*SafeLoader)[^)]*\)",
            r"ObjectInputStream\s*\(",
            r"unserialize\s*\(",
        ),
        suggestion="改用 json / msgpack 等纯数据格式；YAML 用 yaml.safe_load，确有需要再自定义受限 Loader。",
        confidence=0.85,
        description="pickle 反序列化会执行 __reduce__，等于把构造函数交给数据提供方。",
    ),
    Pattern(
        id="SEC106",
        title="关闭 TLS 证书校验",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"verify\s*=\s*False",
            r"ssl\._create_unverified_context\s*\(",
            r"InsecureSkipVerify\s*:\s*true",
            r"rejectUnauthorized\s*:\s*false",
            r"(?:curl|CURLOPT_SSL_VERIFYPEER)[^\n]*\b0\b",
        ),
        suggestion="保留证书校验；内网自签证书应把 CA 加入信任链，而不是关掉校验。",
        confidence=0.9,
        description="关掉校验后中间人可透明替换内容，HTTPS 退化成明文。",
    ),
    Pattern(
        id="SEC107",
        title="口令使用弱哈希算法",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"(?:password|passwd|pwd|secret)[A-Za-z_]*\s*=\s*(?:hashlib\.)?(?:md5|sha1)\s*\(",
            r"(?:md5|sha1)\s*\(\s*(?:password|passwd|pwd|secret)",
            r"MessageDigest\.getInstance\s*\(\s*[\"'](?:MD5|SHA-?1)[\"']",
            r"(?:md5|sha1)\s*\(\s*[\"'][^\"']*[\"']\s*\)",
        ),
        suggestion="口令哈希用 bcrypt / scrypt / Argon2（或 PBKDF2 加足够迭代次数），并加盐。",
        confidence=0.8,
        description="MD5/SHA1 速度极快，GPU 每秒可试算数十亿次，口令空间很快被穷举。",
    ),
    Pattern(
        id="SEC108",
        title="SSRF：请求地址来自外部输入",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            # 只靠「调用了一个发请求的函数」判 SSRF 会大量误报：
            # requests.get(url) 里的 url 完全可能是个本地常量或已校验的值。
            # 单行正则做不到数据流分析，所以这里要求**变量名本身带外部来源信号**，
            # 宁可漏掉一部分真问题，也不能在干净代码上乱报（误报会直接摧毁工具可信度）。
            # 要覆盖剩下的情况，需要引入污点追踪，这已在 README 的后续计划中说明。
            r"(?:requests|httpx)\.(?:get|post|put|delete|request)\s*\(\s*[a-z_]*(?:user|callback|redirect|target|external|remote|untrusted|frontend|client|input|param|arg)[a-z_]*",
            r"(?:requests|httpx)\.(?:get|post|put|delete|request)\s*\(\s*(?:request\.|req\.|params\[|payload\[|body\[|args\[|query\[)",
            r"(?:fetch|axios(?:\.\w+)?)\s*\(\s*[a-z_]*(?:user|callback|redirect|target|external|remote|param|input)[a-z_]*",
            r"urlopen\s*\(\s*[a-z_]*(?:user|callback|redirect|target|external|remote|param|input)[a-z_]*",
            r"new\s+URL\s*\(\s*(?:request\.|req\.|params\[|payload\[)",
        ),
        suggestion="对目标地址做白名单域名校验，解析后检查 IP 不落在内网段，并禁用重定向跟随。",
        confidence=0.6,
        description="攻击者可让服务端去请求 169.254.169.254 等元数据地址，窃取云上凭据。",
    ),
    Pattern(
        id="SEC109",
        title="路径穿越：拼接外部输入构造文件路径",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"open\s*\(\s*(?:os\.path\.join\s*\([^)]*(?:filename|path|name|file|input)|[a-z_]*(?:filename|userfile|user_path|upload_name))",
            r"os\.path\.join\s*\([^)]*(?:request|params|args|user|input|filename)",
            r"Path\s*\(\s*(?:filename|user_path|request\.)",
            r"\.\./\.\./",
            r"readFile(?:Sync)?\s*\(\s*(?:req\.|params\.|query\.)",
        ),
        suggestion="用 os.path.realpath 解析后校验结果仍位于预期根目录内；更稳妥是只用白名单文件名。",
        extensions=(".py", ".js", ".ts", ".java", ".go", ".php"),
        confidence=0.75,
        description="`../../etc/passwd` 可越出预期目录读取任意文件，乃至覆盖配置。",
    ),
    Pattern(
        id="SEC110",
        title="调试开关或详细报错在生产代码中开启",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        regexes=(
            r"app\.run\([^)]*debug\s*=\s*True",
            r"DEBUG\s*=\s*True",
            r"FLASK_DEBUG\s*=\s*[\"']?1",
            r"app\.debug\s*=\s*True",
            r"debug\s*:\s*true\s*,",
            r"(?:show_stacktrace|APP_DEBUG|display_errors)\s*[:=]\s*(?:true|True|On|1)",
        ),
        suggestion="调试开关由环境变量控制，生产环境固定关闭；错误详情只写日志，不回给客户端。",
        confidence=0.8,
        description="Werkzeug 调试页带交互式控制台，可直接执行任意代码；堆栈信息也会泄漏内部结构。",
    ),
    Pattern(
        id="SEC111",
        title="敏感信息写入日志",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        regexes=(
            r"(?:log(?:ger)?|console|print|System\.out)[\w.]*\s*\([^)]*\b(?:password|passwd|pwd|token|secret|api[_-]?key|credential|authorization)\b",
            r"(?:password|token|secret|api[_-]?key)\s*=\s*[^,)]*\b(?:log|print|console)\b",
        ),
        suggestion="日志里对敏感字段做脱敏（只留前后各 2 位），或整字段屏蔽。",
        confidence=0.75,
        description="日志通常被集中采集且访问权限更宽，明文口令会随日志扩散到多处存储。",
    ),
    Pattern(
        id="SEC112",
        title="JWT 未校验签名或允许 none 算法",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"jwt\.decode\s*\([^)]*verify\s*=\s*False",
            r"jwt\.decode\s*\([^)]*options\s*=\s*\{[^}]*verify_signature[\"']?\s*:\s*False",
            r"algorithms\s*=\s*\[\s*[\"']none[\"']",
            r"verify_signature[\"']?\s*:\s*False",
        ),
        suggestion="显式指定算法白名单（如 ['RS256']）并保持签名校验开启；算法不要从 token 头部读取。",
        confidence=0.9,
        description="不校验签名等于任何人自签一个 token 就能冒充任意用户。",
    ),
    Pattern(
        id="SEC113",
        title="CORS 通配来源与凭据同时开启",
        category=Category.SECURITY,
        severity=Severity.MEDIUM,
        regexes=(
            r"allow_origins\s*=\s*\[\s*[\"']\*[\"']",
            r"Access-Control-Allow-Origin[\"']?\s*[,:]\s*[\"']\*[\"']",
            r"origin\s*:\s*[\"']\*[\"']\s*,\s*credentials\s*:\s*true",
            r"setHeader\s*\(\s*[\"']Access-Control-Allow-Origin[\"']\s*,\s*[\"']\*[\"']",
        ),
        suggestion="把来源限制为明确白名单；带凭据的跨域请求不允许使用 * 通配。",
        confidence=0.65,
        description="通配来源让任意站点都能带着用户凭据调用你的接口，等价于交出 CSRF 防线。",
    ),
    Pattern(
        id="SEC114",
        title="服务端模板注入",
        category=Category.SECURITY,
        severity=Severity.HIGH,
        regexes=(
            r"render_template_string\s*\(",
            r"jinja2\.Template\s*\(\s*(?![\"']\s*[\"'])(?:[a-z_]|f[\"'])",
            r"Template\s*\(\s*f[\"']",
            r"\.render\s*\(\s*(?:request|params|args)\b",
        ),
        suggestion="模板内容一律来自受控的模板文件，变量走上下文注入，不要用字符串拼模板。",
        confidence=0.8,
        description="Jinja2 模板里 `{{7*7}}` 会被求值，攻击者可逐步升级到任意命令执行。",
    ),
)


# --------------------------------------------------------------------------
# 性能维度：6 类
# --------------------------------------------------------------------------
_PERFORMANCE: tuple[Pattern, ...] = (
    Pattern(
        id="PERF201",
        title="循环内逐条查询数据库（N+1）",
        category=Category.PERFORMANCE,
        severity=Severity.MEDIUM,
        regexes=(
            r"for\s+\w+\s+in\s+.*:\s*\n[^\n]*(?:\.query|\.filter|\.get|\.find|\.execute|\.fetch|Query\b)",
            r"(?:for|while)\s*\([^)]*\)\s*\{[^}]*(?:query|find|select|execute)\s*\(",
            r"for\s+\w+\s+in\s+.*:\s*\n\s*(?:await\s+)?[a-z_]*(?:fetch|load|query)\w*\s*\(",
        ),
        suggestion="改成一次 IN 查询把关联数据全取回来（或 select_related / join），把 N 次往返压成 1 次。",
        extensions=(".py", ".js", ".ts", ".java", ".go", ".rb", ".php"),
        needs_context=True,
        confidence=0.7,
        description="循环里各查一次数据库，网络往返次数随数据量线性增长，是接口变慢最常见的原因。",
    ),
    Pattern(
        id="PERF202",
        title="异步函数中调用同步阻塞接口",
        category=Category.PERFORMANCE,
        severity=Severity.MEDIUM,
        regexes=(
            r"(?:async\s+def|async\s+function)\s+[\s\S]{0,400}?\b(?:time\.sleep\s*\(|requests\.(?:get|post|put|delete)\s*\(|urlopen\s*\(|\.read\s*\(\s*\))",
        ),
        suggestion="换成异步等价物（asyncio.sleep / httpx.AsyncClient），真正同步的调用用 run_in_executor 丢到线程池。",
        extensions=(".py", ".js", ".ts"),
        needs_context=True,
        confidence=0.75,
        description="同步阻塞调用会卡住整个事件循环，同一进程内其他请求全部被拖住。",
    ),
    Pattern(
        id="PERF203",
        title="循环内累加字符串",
        category=Category.PERFORMANCE,
        severity=Severity.LOW,
        regexes=(
            r"for\s+\w+\s+in\s+.*:\s*\n\s*[a-z_]\w*\s*\+=\s*(?:[\"'][^\"']*[\"']|str\s*\(|f[\"'])",
            r"(?:for|while)\s*\([^)]*\)\s*\{[^}]*\w+\s*\+=\s*[\"']",
            r"for\s+\w+\s+in\s+.*:\s*\n\s*[a-z_]\w*\s*=\s*[a-z_]\w*\s*\+\s*(?:[\"']|str\s*\()",
        ),
        suggestion="收集到 list 里最后 ''.join(parts)，避免每轮都重新分配一次整串。",
        extensions=(".py", ".js", ".ts", ".java"),
        needs_context=True,
        confidence=0.6,
        description="字符串不可变，每轮 += 都复制整串，整体复杂度退化到 O(n²)。",
    ),
    Pattern(
        id="PERF204",
        title="未分页的全量加载",
        category=Category.PERFORMANCE,
        severity=Severity.MEDIUM,
        regexes=(
            # 负向先行断言：同一行已出现 limit / offset 等分页信号时不算全量加载。
            # 「加了分页反而被误报」是最伤可信度的一类误报。
            r"^(?!.*\b(?:limit|offset|first|take|top|paginate|batch_size|fetchmany|page_size)\b)"
            r"[^\n]*\.(?:all|findall|find_all|getall|toList)\s*\(\s*\)\s*\)?\s*$",
            r"^(?!.*\b(?:limit|offset|page_size)\b)"
            r"(?:SELECT\s+\*|select\s+\*)\s+FROM\s+\w+\s*[\"']?\s*\)?\s*$",
            r"^(?!.*\b(?:limit|batch_size)\b)[^\n]*\.(?:find|scan)\s*\(\s*\{\s*\}\s*\)\s*$",
        ),
        suggestion="加 limit/offset 或游标分页；接口层暴露 page_size 参数并设上限。",
        extensions=(".py", ".js", ".ts", ".java", ".sql", ".go"),
        confidence=0.55,
        description="数据量增长后单次全量查询会撑爆内存，接口响应时间也随之失控。",
    ),
    Pattern(
        id="PERF205",
        title="循环内重复构造昂贵对象",
        category=Category.PERFORMANCE,
        severity=Severity.LOW,
        regexes=(
            r"for\s+\w+\s+in\s+.*:\s*\n[^\n]*(?:re\.compile\s*\(|create_engine\s*\(|\.connect\s*\(|getConnection\s*\(|DriverManager\.getConnection|\.Client\s*\(|\.Session\s*\()",
            r"(?:for|while)\s*\([^)]*\)\s*\{[^}]*new\s+(?:HttpClient|Connection|Thread|SimpleDateFormat)",
        ),
        suggestion="把连接、正则、客户端提到循环外复用；确实需要并发时用连接池。",
        extensions=(".py", ".js", ".ts", ".java", ".go"),
        needs_context=True,
        confidence=0.7,
        description="建连接和编译正则有固定开销，放进循环后开销乘上迭代次数。",
    ),
    Pattern(
        id="PERF206",
        title="双层嵌套循环做线性查找",
        category=Category.PERFORMANCE,
        severity=Severity.LOW,
        regexes=(
            r"for\s+\w+\s+in\s+[^:]+:\s*\n\s+for\s+\w+\s+in\s+[^:]+:\s*\n\s+if\s+[\w.\[\]()\"']+\s*==\s*[\w.\[\]()\"']+\s*:",
            r"for\s*\([^)]*\)\s*\{[^}]{0,200}?for\s*\([^)]*\)\s*\{[^}]{0,200}?\.(?:includes|contains|indexOf)\s*\(",
        ),
        suggestion="把内层待查集合先建成 set / dict，把 O(n·m) 降到 O(n)。",
        extensions=(".py", ".js", ".ts", ".java", ".go"),
        needs_context=True,
        confidence=0.6,
        description="嵌套遍历做成员判断，数据量上去后耗时呈平方增长。",
    ),
)


# --------------------------------------------------------------------------
# 规范维度：8 类
# --------------------------------------------------------------------------
_CONVENTION: tuple[Pattern, ...] = (
    Pattern(
        id="CONV301",
        title="静默吞掉异常",
        category=Category.CONVENTION,
        severity=Severity.MEDIUM,
        regexes=(
            # 不用 $ 锚点：needs_context 模式下匹配的是多行窗口，
            # 非 MULTILINE 的 $ 只匹配字符串末尾，会把跨行写法全部漏掉
            r"except[^\n:]*:\s*(?:pass|\.\.\.)",
            r"except[^\n:]*:\s*\n\s*(?:pass|\.\.\.)",
            r"catch\s*\([^)]*\)\s*\{\s*\}",
            r"except\s+Exception\s*:\s*\n\s*(?:pass|continue)",
        ),
        needs_context=True,
        suggestion="至少记录日志（含上下文），或改成向上抛出；确实可忽略的异常要写明为什么。",
        confidence=0.85,
        description="异常被吞掉后故障变成静默错误，排查时没有任何线索。",
    ),
    Pattern(
        id="CONV302",
        title="裸 except / 捕获所有异常",
        category=Category.CONVENTION,
        severity=Severity.MEDIUM,
        regexes=(
            r"^\s*except\s*:\s*$",
            r"except\s+BaseException\s*:",
            r"except\s+Exception\s*:\s*$",
        ),
        suggestion="只捕获确实能处理的异常类型；确实要兜底时捕获后再 re-raise 或明确降级。",
        extensions=_PY,
        confidence=0.6,
        description="裸 except 会连 KeyboardInterrupt、SystemExit 一起吞掉，也会掩盖代码缺陷。",
    ),
    Pattern(
        id="CONV303",
        title="调试用打印语句残留",
        category=Category.CONVENTION,
        severity=Severity.LOW,
        regexes=(
            r"^\s*print\s*\(\s*(?:f?[\"']|\w+\s*[,)])",
            r"^\s*console\.(?:log|debug)\s*\(",
            r"^\s*System\.out\.println\s*\(",
            r"^\s*fmt\.Print(?:ln|f)?\s*\(",
        ),
        suggestion="换成项目统一的 logger，并按级别输出；调试语句在提交前清掉。",
        confidence=0.5,
        description="print 无法分级、无法关闭、无法结构化采集，生产环境里是噪声也是性能损耗。",
    ),
    Pattern(
        id="CONV304",
        title="遗留的 TODO / FIXME / 占位实现",
        category=Category.CONVENTION,
        severity=Severity.LOW,
        regexes=(
            r"(?:#|//|/\*)\s*(?:TODO|FIXME|XXX|HACK)\b",
            r"(?:#|//)\s*(?:暂时|先这样|临时|待补)\b",
            r"raise\s+NotImplementedError\s*\(",
            r"throw\s+new\s+UnsupportedOperationException\s*\(",
        ),
        suggestion="转成 issue 跟踪并注明负责人与期限；不能留待办进主干分支。",
        confidence=0.55,
        # 这条规则检查的就是注释内容，不能跳过注释
        ignore_comments=False,
        description="未跟踪的 TODO 会一直留在代码里，成为没人认领的技术债。",
    ),
    Pattern(
        id="CONV305",
        title="可变对象作默认参数",
        category=Category.CONVENTION,
        severity=Severity.MEDIUM,
        regexes=(
            r"def\s+\w+\s*\([^)]*=\s*(?:\[\s*\]|\{\s*\}|set\s*\(\s*\)|dict\s*\(\s*\)|list\s*\(\s*\))",
            r"function\s+\w+\s*\([^)]*=\s*(?:\[\s*\]|\{\s*\})",
        ),
        suggestion="默认值用 None，函数体内 `if x is None: x = []` 再初始化。",
        extensions=_PY,
        confidence=0.9,
        description="默认参数在函数定义时求值一次，所有调用共享同一个列表，调用之间互相污染。",
    ),
    Pattern(
        id="CONV306",
        title="用 == / != 与 None 或 True 比较",
        category=Category.CONVENTION,
        severity=Severity.LOW,
        regexes=(
            r"[!=]=\s*None\b",
            r"\bNone\s*[!=]=",
            r"[!=]=\s*True\b",
            r"\bTrue\s*[!=]=",
        ),
        suggestion="判 None 用 `is None` / `is not None`；判真值直接用 `if x:` 或 `if not x:`。",
        extensions=_PY,
        confidence=0.7,
        description="== 会走 __eq__，某些对象（如 numpy 数组、ORM 字段）上返回的不是布尔值，条件判断会出错。",
    ),
    Pattern(
        id="CONV307",
        title="用 assert 做运行时校验",
        category=Category.CONVENTION,
        severity=Severity.MEDIUM,
        regexes=(
            r"^\s*assert\s+[^,\n]+,\s*[\"']",
            r"^\s*assert\s+(?:isinstance|len|hasattr)\s*\(",
            r"^\s*assert\s+.*\bis\s+not\s+None",
        ),
        suggestion="对外部输入的校验用显式 if + raise ValueError；assert 只用于内部不变式。",
        extensions=_PY,
        confidence=0.6,
        description="python -O 会整体移除 assert，把校验逻辑一起删掉；它也不该用来处理外部输入。",
    ),
    Pattern(
        id="CONV308",
        title="文件句柄未使用上下文管理器",
        category=Category.CONVENTION,
        severity=Severity.MEDIUM,
        regexes=(
            r"^\s*\w+\s*=\s*open\s*\(",
            r"^\s*(?:f|fp|fh|file)\s*=\s*io\.open\s*\(",
            r"=\s*fs\.openSync\s*\(",
            r"new\s+FileInputStream\s*\(",
            r"new\s+FileReader\s*\(",
        ),
        suggestion="用 with open(...) as f / try-with-resources / defer f.Close()，确保异常路径也会关闭。",
        confidence=0.7,
        description="中途抛异常时句柄不会释放，长跑服务会逐步耗尽文件描述符。",
    ),
)


PATTERNS: tuple[Pattern, ...] = _SECURITY + _PERFORMANCE + _CONVENTION

PATTERN_INDEX: dict[str, Pattern] = {p.id: p for p in PATTERNS}


def get_pattern(pid: str) -> Pattern | None:
    return PATTERN_INDEX.get(pid)


def catalog() -> list[dict]:
    """给 `--rules` 命令用的目录清单。"""
    return [
        {
            "id": p.id,
            "title": p.title,
            "category": p.category.value,
            "severity": p.severity.value,
            "extensions": list(p.extensions) or ["*"],
            "needs_context": p.needs_context,
            "confidence": p.confidence,
            "description": p.description,
        }
        for p in PATTERNS
    ]


def stats_by_category() -> dict[str, int]:
    out = {c.value: 0 for c in Category}
    for p in PATTERNS:
        out[p.category.value] += 1
    return out
