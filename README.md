# PR 代码审查 Agent

自动审查 Git 平台 Pull Request 的 Agent：解析 diff 后按 **安全 / 性能 / 规范** 三个维度审查，
把意见以**带行号的行内评论**回评到 PR 上，并跑 GitHub Action 全流程 CI。

**运行时零第三方依赖** —— 只用 Python 标准库。clone 下来 `python main.py` 就能跑，
不需要 `pip install`，也不会有依赖版本冲突。

```bash
# 不配任何 API Key 也能立刻看到效果（离线替身模式）
python main.py --diff examples/demo.patch --mock

# 只看规则库，零 token 成本
python main.py --diff examples/demo.patch --rules-only

# 审查真实的 GitHub PR（需要 .env 里的 GITHUB_TOKEN）
python main.py --pr owner/repo#42 --mock --out runs
python main.py --pr owner/repo#42 --post --apply   # 真正回评
```

---

## 为什么要分「规则」和「模型」两层

全交给模型：成本高、结果不可复现、换个模型结论就变。
全交给规则：遇到「这个事务边界不对」就瞎了。

所以按**判据是否可形式化**切分：

| 层 | 负责 | 特点 |
| --- | --- | --- |
| 规则库（28 类模式） | 正则能抓住、有明确判据的缺陷 | 零成本、可复现、可写进测试、证据是原文片段 |
| 大模型 | 需要理解语义的问题（事务边界、超时缺失、参数校验） | 有成本，但能覆盖规则永远做不到的部分 |

两层的结果各自统计、各自评测。混在一起算出来的数字没法指导改进 ——
分不清是规则库该补，还是提示词该改。

---

## 三道闸门：这个项目真正的技术含量

### 1. 分片闸门 —— 按行切，不按 hunk 切

一个**新增文件**在 diff 里就是**一个巨大的 hunk**。按 hunk 分片的话整个文件会挤进同一个分片，
直接撑爆上下文窗口；而且模型看到几千行时会「只读开头」。

所以分片粒度做到**行**，让任意大小的文件都切成等长的块。代价是分片边界可能落在函数中间 ——
但「模型看不到完整函数」最多少提一条意见，「撑爆上下文」会让整个分片直接失败。前者是更小的代价。

### 2. 锚定闸门 —— 无行号不评论

模型非常擅长编一个**看起来合理**的行号。如果直接拿它的输出去调 GitHub 的行内评论 API，
轻则 422 报错，重则评论挂到**错误的代码行**上 —— 后者更糟，因为它看起来像胡说八道。

所以每条模型意见的行号都必须存在于 diff 的新增行集合里，不在的直接丢弃并计数。
另有两条约束：

- **文件路径以分片为准，不信模型给的路径**。模型编路径比编行号更容易，而且编出来的路径
  可能指向仓库里真实存在的另一个文件。
- **强制引用原文**，`evidence` 字段用于人工快速复核。

### 3. 去重闸门 —— 按 (文件, 行, 维度) 合并

规则和模型经常同时命中同一处。合并时保留规则那条（证据更硬、可复现），
并在报告里标记 `🔁 双通道一致`，置信度上调。

这里踩过一个坑：跨行模式（「循环内查库」）起初会**把同一个 N+1 报 5 次** ——
因为循环体内每一行的上下文窗口都包含那个 `for`。修法是把锚点定在**匹配起始行**
而不是当前遍历行，重复项自然收敛成同一个 `(规则, 行号)`。
`tests/test_rules_context.py` 专门钉住了这个行为。

---

## 实测数据

> 口径说明：**调参集**上的分数是自己出题自己答，没有信息量；
> 只有**留出集**的数字可以对外说。

```
【规则召回】合计 30/30 = 100%
   · 调参集  28/28 = 100%（15 个用例，模式库是照着它调的，参考价值有限）
   · 留出集   2/2  = 100%（2 个用例，未参与调参）

【负数误报】0 条意见 / 5 个干净用例（0 个用例被误报）
【锚定有效率】13/13 = 100%（被丢弃的都是行号不存在的幻觉）
【语义缺陷命中率】1/1 = 100%（规则抓不到、只能靠模型理解的缺陷）
【单元测试】314 项全通过
```

**必须说明的局限性**：留出集只有 2 个用例，样本量太小，100% 不具备统计意义。
它证明的是「语料和方法论是通的」，而不是「这个工具准确率 100%」。
真正可信的做法是持续往 `eval/corpus/` 加**真实 PR** 的 diff 并标注 —— 这是后续计划的第一项。

**锚定有效率**同样要谨慎读：它是在离线替身模式下测的，而替身只会输出输入里真实存在的行号，
所以 100% 是**链路正确性**的证明，不是模型幻觉率的测量。要测幻觉率必须用真实模型跑。

负例对照是这个评测里最有价值的部分：`h03_comment_traps` 里注释写满了
`# 不要用 os.system(cmd)`、`# 也不要写 verify=False` 这类**反例说明**，
一条都不该报。为此规则引擎实现了注释守卫（`CONV304` 检查的就是注释内容，显式关闭该行为）。

---

## 目录结构

```
pr-review-agent/
├── main.py                     入口（把 src/ 挂进 sys.path 后转交 CLI）
├── src/pagent/
│   ├── diffparse.py            unified diff 解析 → 带新文件行号的结构化数据
│   ├── patterns.py             28 类缺陷模式（安全 14 / 性能 6 / 规范 8）
│   ├── rules.py                规则引擎（注释守卫、扩展名过滤、上下文匹配）
│   ├── retrieval.py            仓库规范检索（BM25，中文按字二元组切分）
│   ├── llm.py                  LLM 客户端（退避重试、json_mode 降级、离线替身）
│   ├── reviewer.py             审查编排：分片 → 检索 → 调用 → 锚定校验 → 合并
│   ├── github.py               GitHub REST 客户端（默认 dry-run，整批失败降级为逐条）
│   ├── mcp.py                  自研 MCP stdio 客户端（JSON-RPC 2.0 全套握手）
│   ├── report.py               Markdown / JSON / PR 评论 / 终端四种输出
│   └── cli.py                  命令行入口
├── tests/                      314 项单元测试
├── eval/
│   ├── build_corpus.py         生成标注语料（difflib，不依赖 git）
│   ├── corpus/*.patch          22 个标注用例
│   ├── labels.json             金标：期望命中的规则号 + 负例标记 + 留出标记
│   └── run_eval.py             评测脚本（召回 / 误报 / 锚定 / 语义 四项指标）
├── examples/
│   ├── demo.patch              演示用 diff（含注释陷阱）
│   └── conventions/*.md        规范文档（RAG 检索语料）
└── .github/workflows/pr-review.yml   测试 + 自动审查 全流程 CI
```

---

## 设计取舍与已知局限

**1. SSRF 规则会漏报。** 单行正则判不出「这个 URL 来自哪里」——`requests.get(url)` 里的 `url`
完全可能是本地常量。所以 `SEC108` 要求**变量名本身带外部来源信号**
（`callback_url` / `user_url` / `req.query` 等）。
宁可在干净代码上漏掉，也不能乱报：**误报会直接摧毁工具的可信度**，
而漏报只是少发现一个问题。要覆盖剩下的情况需要引入污点追踪。

**2. 分片会切断函数上下文。** 见上文权衡。改进方向是按语法块（AST）切分。

**3. 规则库和语料是一起写的。** 所以调参集上的 100% 毫无意义，只留出集能看。
缓解办法是持续用真实 PR 扩充留出集。

**4. MCP 通道只读。** `mcp.py` 实现了完整的 MCP stdio 客户端，但读数据走 MCP、
**写回一律走直连 REST**。让一个自动工具通过 MCP 往 PR 写内容，权限面太大且难以审计。

**5. 离线替身不是质量代理。** `MockLLMClient` 会真的解析输入里的行号、跑一组语义启发式，
足以把「分片 → 锚定 → 去重 → 渲染」整条链路跑通，但它的误报率不能代表真实模型。

---

## 后续计划

- [ ] 用真实 PR 的 diff 扩充留出集到 50+ 用例，拿到有统计意义的召回率
- [ ] 引入污点追踪，替换 SSRF 的变量名启发式
- [ ] 分片改为按 AST 语法块切分，保持函数完整性
- [ ] 修复建议支持一键应用（生成可 patch 的 unified diff）
- [ ] 统计人工 Reviewer 与自动意见的一致率（现在缺人工标注数据，不编这个数字）

---

## 环境变量

复制 `.env.example` 为 `.env` 后填写。**`.env` 已在 `.gitignore` 中排除。**

| 变量 | 说明 |
| --- | --- |
| `LLM_API_KEY` | 不填也能跑（`--mock` 或 `--rules-only`） |
| `LLM_BASE_URL` / `LLM_MODEL` | OpenAI 兼容协议，常见供应商见 `.env.example` |
| `GITHUB_TOKEN` | 读取 PR / 回评需要。Fine-grained token 即可，权限：Contents 读、Pull requests 读写 |
| `MCP_GITHUB_SERVER_CMD` | 配置后走 MCP 通道读取 PR |
| `SHARD_MAX_CHARS` | 单分片字符上限，默认 6000 |
| `REQUIRE_LINE_ANCHOR` | 是否强制「无行号不评论」，默认 `true`。**不建议关闭** |

---

## License

MIT

---

## 检索后端：三档可切换（新增）

`retrieval.py` 里的 `ConventionStore` 是**纯标准库的 BM25**，它的价值是零依赖、可复现。
但 BM25 只看词面、不看语义，而规范文档的查询经常是语义查询。

于是加了 `vector_store.py`，提供与 `ConventionStore` **同一个 `Retriever` 协议**的三档实现：

| backend | 说明 | 依赖 |
| --- | --- | --- |
| `bm25` | 纯标准库 BM25（**默认**） | 无 |
| `vector` | 仅 Chroma 向量检索 | `pip install chromadb` |
| `hybrid` | BM25 + 向量，**RRF 融合** | `pip install chromadb` |

用环境变量切换，`reviewer.py` 一行都不用改：

```bash
RETRIEVAL_BACKEND=hybrid python main.py --diff examples/demo.patch --mock
```

### 为什么用 RRF 而不是加权求和

BM25 的分数是无界的（可能 0.3，也可能 30），余弦相似度是 [-1, 1]。直接加权求和要先把
两路分布拉到同一尺度，反而丢掉了「BM25 认为这条异常匹配」这种信息。
RRF 只用**排名**，天然规避量纲问题：

```
score(d) = Σ_r  1 / (k + rank_r(d))        # k = 60
```

### 两个实现细节

- **Embedding 用确定性哈希向量**（feature hashing + sign hashing + L2 归一化，
  特征含英文词 / 中文二元组 / 字符级 3-gram）。理由：评测要可复现，真实 embedding API
  每次调用可能有微小数值差异，而「留出集召回 2/2」这类结论必须跑一百次都一样。
  需要真语义时实现 `Embedder` 协议接入 bge / text-embedding-3 即可。
- **向量侧异常自动降级回 BM25**，而不是让整次审查失败。可用性优先。

> 踩过的坑：`chromadb.EphemeralClient()` 每次返回新对象，但**底层内存 store 是共享的**，
> 所以同名 collection 其实是同一个集合。早期实现用 `inline#0`、`inline#1` 当 id，
> 第二个实例就会撞上第一个实例的 id —— 而 Chroma 对重复 id 的 add 是**静默忽略**的。
> 这个 bug 只在同一进程先后建过两个实例时出现，单测单独跑不触发、整包跑才暴露。
> 修法：id 加实例前缀 + 用 `where={"instance": ...}` 精确过滤。
> `tests/test_vector_store.py` 里有两条回归测试钉住它。

---

## 服务化：FastAPI（新增）

```bash
pip install fastapi uvicorn
uvicorn pagent.api:app --reload --port 8000
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/health` | 健康检查（含 Chroma 可用性） |
| GET | `/backends` | 可用检索后端 |
| POST | `/review` | 审查一段 diff |
| GET | `/openapi.json` | 自动生成的 OpenAPI 文档（`/docs` 有交互界面） |

```bash
curl -X POST http://127.0.0.1:8000/review   -H 'Content-Type: application/json'   -d '{"diff": "..."}'
```

API 层默认 `confirm=None`，**敏感操作在服务端一律拒绝**（没有交互式确认的环境里不该放行）。

---

## 容器化（新增）

```bash
docker build -t pr-review-agent .
docker run -p 8000:8000 pr-review-agent -m uvicorn pagent.api:app --host 0.0.0.0
```

因为本项目**运行时零第三方依赖**，镜像里不需要 `pip install` 任何东西 —— 这是零依赖
设计的一个具体收益。镜像用非 root 用户运行，默认 `MOCK=true OFFLINE=true DRY_RUN=true`。

配合仓库根目录的 `docker-compose.yml` 可与 browser-agent 一起编排。

### 构建踩过的坑：`doctor` 忽略环境变量

`cmd_doctor` 里原来写的是 `st.mock = args.mock`，这行会把环境变量读进来的
`MOCK=true` **覆盖回 `False`**（因为容器里没人传 `--mock` 这个 CLI 参数）。
后果是：容器自检一边显示「模式：真实调用」，一边真的向 LLM 发了一次请求 ——
在声称 `OFFLINE` 的镜像里既误导又出网。`[LLM 连通性]` 那一段同理，原来判断的是
`args.mock` 而不是合并后的 `st.mock`。

现在两处都改成「只允许 `--mock` 把开关打开，不允许关掉」，与 `cmd_review` 里
既有的写法保持一致。这个坑的通用形态值得记：**当同一个开关既有 CLI flag 又有
环境变量两个来源时，赋值前先想清楚谁覆盖谁。**
