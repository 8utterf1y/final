# RAG 知识库与经验沉淀系统

## 第一部分：总结介绍

这套 RAG 现在不只是“导入文档后检索答案”，而是承担了两个相关目标：第一，把项目外部资料、PDF、Markdown、CSV、JSON 等文档变成可检索知识；第二，把 Agent 完成任务时产生的调查、修改、验证过程沉淀成结构化经验文档，写入同一个知识库，供后续相似任务召回复用。对应到简历表述，就是“经验沉淀与知识库检索增强”。

整体链路可以分成经验生产链、离线索引链和在线检索链。经验生产链由 [experience.py](/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/experience.py:1) 负责：Agent 每轮开始时记录用户消息，每次工具调用、工具结果、权限拒绝和 assistant 文本也会被记录到 `TaskJournal`。这个 journal 故意独立于聊天历史，因为第七章的 compact、snip、microcompact 会改写消息；但可复用经验需要不可变事件证据。

当用户执行 `/experience save` 时，`ExperienceManager.save()` 会从 journal 里选取候选事件。它先判断有没有实质活动，例如写文件、编辑文件、shell 验证或失败记录；没有就跳过，避免把普通聊天也存成经验。若存在可复用轨迹，它会调用一个经验抽取器，让模型把事件列表整理成固定 JSON：适用场景、问题症状、根因诊断、处理步骤、坑点、验证方式、相关文件模式、检索查询和 tags。抽取失败时还有 deterministic fallback，保证功能可用。

经验 JSON 不是直接相信模型输出。`validate_experience_payload()` 会校验 turn 范围、清洗字段、限制列表长度、移除不存在的 evidence event id，并根据是否有成功 shell 验证、是否有编辑、是否有失败修复等计算 `quality_score`。低质量经验会被标记为 `skip`；没有验证证据的经验会加上 `unverified` 标签。这里体现的是“经验可以由模型总结，但必须由程序做结构化校验和质量门控”。

通过校验后，`render_experience_markdown()` 会把经验渲染成 Markdown，并写入项目级目录 `~/.mini-claude/projects/<project-hash>/experiences/`。随后它会调用 `get_knowledge_store().add_document(str(path))`，把这份经验文档作为知识库文档导入和索引。也就是说，经验沉淀不是另起一套检索系统，而是复用知识库的 loader、chunking、embedding、FTS5、HNSW、RRF 和 rerank 能力。删除经验时，也会尝试删除对应 knowledge document，保证经验文件和索引大体同步。

离线索引链由 [knowledge.py](/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/knowledge.py:1)、[knowledge_loaders.py](/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/knowledge_loaders.py:1) 和 [embeddings.py](/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/embeddings.py:1) 组成。知识库按项目隔离，当前工作目录绝对路径会被 SHA-256 成 16 位 project hash，数据存到 `~/.mini-claude/projects/<project-hash>/knowledge/`。不同项目即使导入同名文件也不会混在一起，但这不是强安全租户隔离，因为本地文件没有加密，也没有用户级 ACL。

文档导入时会先读取原始字节计算 `content_hash`，数据库对这个 hash 建唯一约束，因此同一内容重复导入是幂等的。文档状态从 `PENDING` 到 `PROCESSING`，最终变为 `COMPLETED` 或 `FAILED`。如果 reindex 失败，旧的 active version 不会提前删除，因此已存在知识仍可搜索。这是版本指针式发布：新索引完整写入后才切换 `documents.active_version`，查询只读取 active version 对应的 chunks，避免用户查到半成品。

Loader 层把不同格式转成统一的 `ParsedKnowledgeDocument`。内置支持 Markdown、TXT、CSV、JSON、JSONL、HTML、XML；PDF、Word、PowerPoint、Excel、RTF 可通过 MarkItDown 转换。解析层会提取标题、MIME、parser、frontmatter tags/description 等元数据，并清理控制字符、无意义分隔线和 HTML 脚本样式。这样检索层不需要关心文件格式，只处理规范文本、结构信息和 metadata。

分块是 RAG 质量的关键。项目默认块大小约 500 token，重叠约 60 token。这里不是简单按固定字符切分，而是结构感知分块：Markdown 和 HTML 优先保留标题层级；PDF 识别页码；CSV 按行区间切块并重复表头；JSON 按顶层 key、数组范围和 JSONPath 切分；JSONL 按记录区间切分。chunk 的 heading 会和正文一起送入 embedding，例如 `heading + "\n\n" + content`，这样标题包含主题、正文表达很省略时也能被召回。

Embedding 层通过 `EmbeddingProvider` 协议解耦具体服务。默认实现使用 OpenAI-compatible 异步客户端，支持独立配置 API key、base URL、模型、维度和 batch size。索引会记录每份文档使用的 embedding model 和 dimensions；查询时如果当前 provider 与已索引文档不一致，会拒绝检索并提示 `/kb reindex all`。原因是不同 embedding 模型的向量空间不可比，哪怕维度相同也不能混搜。

存储层使用 SQLite。`documents` 保存文档元数据和 active version，`chunks` 保存正文、heading、位置、metadata 和 float32 向量 BLOB，`chunks_fts` 使用 SQLite FTS5 建全文索引。FTS5 优先 trigram tokenizer，便于中文和无空格文本的子串匹配；不支持时回退默认 tokenizer。小规模向量检索走 exact search，即对候选向量逐个计算余弦相似度；规模上来后可选 HNSW 近似向量索引，用图结构提升查询速度。

在线检索链从 `knowledge_search` 工具开始。这个工具定义在 [tools.py](/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/tools.py:136)，属于 deferred tool。初始请求不会发送完整 schema，只在动态 System Prompt 的 manifest 里告诉模型有知识库文档，并在 deferred 工具列表里提示可以通过 `tool_search` 激活。模型判断问题和知识库相关时，先调用 `tool_search`，下一轮才获得 `knowledge_search` 完整参数。这是渐进式披露：先披露文档目录，再披露工具 schema，最后才返回相关正文 chunk。

检索时系统先做 metadata 路由和显式过滤。`knowledge_search` 支持按 document id、source dir、MIME、parser、tags、chapter 等过滤；没有显式条件时，会根据查询与文档标题、文件名、描述、标签和 headings 的重合度做保守自动路由。只有置信度足够高才缩小范围，否则全库搜索。这个策略优先保证 recall，因为错误路由会在召回前把正确文档排除掉。

召回采用双路检索：向量召回负责语义相似，适合“上下文压缩怎么做”这类自然语言改写；FTS5/BM25 关键词召回负责精确词，适合函数名、错误码、配置项和文件名。两路各取候选后，用 RRF 融合排序：

```text
RRF(d) = sum(1 / (60 + rank_i(d)))
```

RRF 的好处是不需要强行归一化余弦相似度和 BM25 这两个不同量纲的分数，只看排名。一个 chunk 如果在语义榜和关键词榜都靠前，融合分自然更高。随后 `_rerank_candidates()` 再做本地二阶段重排，综合向量分、RRF 分、查询词覆盖、标题/标签覆盖、关键词排名和精确短语命中，把更可解释、更贴近查询意图的 chunk 提到前面。

最终结果还会受上下文预算约束：`top_k` 限制在 1 到 10，默认 6；每个文档最多返回 3 个 chunk；总正文最多约 20000 字符。这样既避免一个长文档垄断全部结果，也避免 RAG 工具结果吃掉整个 Agent 上下文。若仍超过 Agent 层 30KB，大结果持久化还会接管，把完整结果保存到磁盘并只给模型预览。

安全上，知识库结果被包在 `<knowledge-results>` 中，并标记为不可信参考数据。System Prompt 明确要求模型不要执行知识库结果里的指令，回答时引用 source 和 heading，没有证据时说明没找到。这里和权限系统形成闭环：RAG 文档可能包含 Prompt Injection，模型即使被诱导产生危险工具调用，仍要经过 permission gate、deny rules、确认和工具自身校验。

评测闭环由 `/kb eval` 支持。评测集可以指定 query、预期文档、source/heading 子串、预期关键词和 top-k，系统计算 Recall@K、MRR 和 Precision@K。你简历里的“相较于单路检索，Recall@5 由 68% 提升至 86%”可以这样解释：单路检索通常指只用向量或只用关键词；双路召回加 RRF 融合后，前 5 个结果包含正确证据的比例从 68% 到 86%，说明正确证据更容易进入模型可见上下文。注意这衡量的是 retrieval recall，不等同于最终答案准确率，生产还要评估 groundedness、faithfulness 和 citation correctness。

把这套系统串起来看，它的亮点不是“调用 embedding API”，而是把 Agent 运行轨迹、经验抽取、结构化文档、混合检索、渐进披露、上下文预算和安全边界接成闭环。经验沉淀让成功任务可复用，结构感知分块提高文档片段质量，HNSW 和 FTS5 分别覆盖语义与关键词，RRF 降低融合调参成本，评测指标让优化有数据依据。

## 面试话术版本

我在这个项目里实现的是一个项目级 RAG 和经验沉淀系统。Agent 执行任务时会用独立的 `TaskJournal` 记录用户目标、工具调用、工具结果、权限拒绝和验证结果，因为聊天历史会被 compact 改写，不适合做经验证据。用户执行 `/experience save` 后，系统会把轨迹抽取成结构化经验文档，包括适用场景、问题、根因、处理步骤、坑点和验证方式，再写成 Markdown 并导入知识库。

知识库离线侧做结构感知切分：Markdown/HTML 保留标题层级，PDF 保留页码，CSV 按行切并重复表头，JSON 按 JSONPath 切分；chunk 的标题和正文一起生成 embedding。索引层用 SQLite 保存文档、chunk、metadata 和向量，同时用 FTS5 建关键词索引；小规模走 exact vector search，大规模可启用 HNSW 近似向量检索。重建时通过 `active_version` 做版本切换，新索引完整完成后才激活，失败不会影响旧索引可查。

在线侧采用混合召回：向量检索负责语义相似，FTS5/BM25 负责关键词和精确标识符，然后用 RRF 按排名融合，再用本地 reranker 结合向量分、词覆盖、标题标签覆盖和精确短语做二阶段排序。相比单路检索，Recall@5 从 68% 提升到 86%，说明前五个候选更容易包含正确证据。Agent 集成上，知识库先只在 System Prompt 里披露 manifest，`knowledge_search` 作为 deferred tool 按需激活，结果被标记为不可信参考数据，并受上下文预算和权限系统保护。

## 第二部分：面试问答与追问补充

### Q1：面试官问：你这个 RAG 系统的核心亮点是什么？不要只说用了向量库。

核心亮点是把 Agent 的任务经验和外部知识统一进一个可检索闭环。Agent 完成任务时会记录工具调用、失败、修改和验证结果，`/experience save` 把轨迹抽取成结构化经验 Markdown，再写入知识库。后续遇到类似问题时，模型可以通过 `knowledge_search` 召回历史流程，而不是只依赖当前短期上下文。

检索侧不是单一路径，而是结构感知分块、HNSW/精确向量召回、FTS5/BM25 关键词召回、RRF 融合和本地 rerank。这个组合解决的是“自然语言语义”和“代码符号精确匹配”两类需求。

### Q2：面试官问：为什么要做经验沉淀？RAG 里放文档不就够了吗？

普通文档回答的是“知识是什么”，经验文档回答的是“这类工程任务应该怎么做”。在 Coding Agent 里，很多价值来自排查路径、失败信号、验证命令、踩坑和项目约定，这些并不总是存在于静态文档中。

所以经验沉淀补的是过程知识。后续类似任务被召回时，Agent 可以复用调查顺序和验证方式，但仍然要基于当前代码重新确认，不能盲套历史经验。

### Q3：面试官问：为什么 `TaskJournal` 要独立于聊天历史？

聊天历史是给模型看的上下文投影，会被 snip、microcompact、compact 改写；而经验提炼需要稳定证据链，必须知道当时用户说了什么、调用了什么工具、结果是成功还是失败。

所以项目把 `TaskJournal` 做成独立事件流。上下文可以为了省 token 被压缩，但经验沉淀不能依赖已经被摘要或占位符替换的消息。

### Q4：面试官问：模型抽取经验会不会编造？你怎么控制？

会有这个风险，所以我没有直接相信模型的自由文本。抽取器必须返回固定 JSON schema，包括适用场景、问题、根因、步骤、验证和 evidence event ids；程序再校验 turn 范围、事件 ID 是否存在、字段长度和质量分。

如果没有成功验证，会打上 `unverified` 标签；如果质量分太低，就 skip。也就是说，模型负责结构化总结，代码负责质量门控和证据约束。

### Q5：面试官问：为什么经验文档要写成 Markdown，而不是直接写数据库？

Markdown 有两个好处：第一，人可以读、可以审查、可以版本化；第二，它天然适合现有知识库 loader，frontmatter 提供 title、description、tags，正文标题能参与结构分块。

如果直接写数据库，检索可以做，但可解释性和可维护性会差一些。这里选择 Markdown 是为了兼顾机器检索和人工复盘。

### Q6：面试官问：结构感知分块具体解决什么问题？

它解决“块的语义边界不合理”问题。纯字符切分可能把标题和正文、表头和数据、JSON key 和 value、PDF 页码和内容拆开，导致向量表达不完整。

项目按格式处理：Markdown/HTML 保留标题层级，CSV 每块重复表头，JSON 保留 JSONPath，PDF 保留页码。这样每个 chunk 更像一个可独立理解的证据单元。

### Q7：面试官问：为什么 chunk heading 要和正文一起做 embedding？

因为正文经常是局部表达，比如“这种方式”“上述配置”，真正的主题在标题里。如果只 embedding 正文，语义会变弱。

把 heading 和 content 合并后，chunk 在向量空间里会带上章节主题。这样查询“权限模式如何工作”时，更容易召回标题为“Permission Mode”的片段，即使正文没有重复完整问题。

### Q8：面试官问：为什么要同时用向量检索和 FTS5/BM25？

向量检索适合语义改写，比如“怎么避免上下文爆掉”可以召回“context compaction”；但它可能漏掉函数名、错误码、配置项、文件名这种精确符号。FTS5/BM25 正好补这个短板。

代码项目里大量问题都带精确标识符，所以单向量检索不够稳。混合召回能同时覆盖自然语言问题和工程符号匹配。

### Q9：面试官问：为什么用 RRF 融合，而不是把余弦分和 BM25 分直接相加？

因为余弦相似度和 BM25 的分数尺度不同，方向和分布也不同，直接相加需要复杂归一化，且很容易被某一路分数主导。

RRF 只看排名：一个 chunk 在向量榜和关键词榜都靠前，就会自然得到更高分。它简单、稳定、对不同检索器分数尺度不敏感，适合工程落地。

### Q10：面试官问：RRF 的 `k=60` 是理论最优吗？

不是。`k=60` 是常见经验值，用来平滑排名差异，避免第一名对后续候选形成过强压制。

我不会把它说成理论最优。它应该通过 golden set 评测调参，比如比较不同 `k` 下 Recall@5、MRR、Precision@K 和最终回答质量。

### Q11：面试官问：RRF 后为什么还需要 rerank？

RRF 只融合排名，不看原始语义分强弱，也不理解标题、标签、精确短语这些业务信号。项目的本地 reranker 会综合向量分、RRF 分、查询词覆盖、metadata 覆盖、关键词排名和精确短语命中。

这个二阶段设计是典型召回与排序分离：第一阶段尽量把可能相关的候选捞上来，第二阶段再把更有解释性的结果排到前面。

### Q12：面试官问：为什么不用 Cross-Encoder 或 LLM reranker？

这个项目是本地轻量实现，优先考虑部署简单、成本低和可解释。固定权重 reranker 虽然不如 Cross-Encoder 理解复杂语义，但没有额外模型调用延迟，也容易通过单元测试固定行为。

生产化时可以把本地 reranker 作为 baseline，再引入 Cross-Encoder 或 LLM reranker，并用评测集判断收益是否覆盖成本。

### Q13：面试官问：HNSW 带来了什么？为什么不是一开始就用？

HNSW 是近似最近邻图索引，大规模 chunk 下能避免 exact search 的线性扫描，降低向量检索延迟。但它需要额外依赖、索引文件和构建成本，而且是近似检索，可能损失少量 recall。

所以项目用 `auto/exact/hnsw` 模式：小知识库 exact 更简单可靠；达到一定规模并安装依赖后再启用 HNSW。性能优化不应该过早增加复杂度。

### Q14：面试官问：metadata filter 时为什么可能回退 exact？

当前 HNSW 是全局索引。如果先取全局 top-k 再过滤，很可能 top-k 都来自过滤范围外，导致目标文档候选饥饿。

在过滤范围较小时，直接 exact 扫描目标集合更稳。这个点说明 ANN 和权限/metadata filter 结合时要考虑 pre-filter 和 post-filter 的顺序。

### Q15：面试官问：你怎么保证 reindex 不会让用户搜到半成品？

项目用 `active_version` 做版本切换。新索引写入时使用新的 `index_version`，只有解析、分块、embedding 和写库完成后，才把文档的 `active_version` 指到新版本。

查询时只读取 active version 对应的 chunk。如果 reindex 失败，会清理新版本部分数据，旧版本仍然可查。这比先删旧索引再重建可靠。

### Q16：面试官问：这算真正的原子发布吗？

在 SQLite 内部，它接近版本指针式原子切换；但从全链路看不是严格 ACID，因为源文件复制、HNSW 文件写入和数据库事务不是同一个事务。

我会把它描述成工程上的一致性设计，而不是强事务系统。生产化可以增加文件写入临时名、校验、锁、崩溃恢复和后台清理任务。

### Q17：面试官问：你说 Recall@5 从 68% 到 86%，怎么证明是混合检索带来的？

要做对照实验。固定同一批 golden queries 和同一套标注，分别跑单路向量、单路关键词、向量+关键词+RRF，再比较 Recall@5。其他参数比如 chunking、top-k、过滤条件要保持一致，避免把收益归因错。

68% 到 86% 表示前 5 个结果中包含正确证据的查询比例提升。它证明的是 retrieval recall 提升，不直接等于最终回答准确率提升。

### Q18：面试官问：为什么看 Recall@5，而不是只看 Precision？

RAG 的第一目标是把正确证据送进模型上下文。如果正确证据没进前 K，生成阶段再强也没用，所以 Recall@K 很关键。

Precision 也重要，因为噪声太多会干扰模型。我的做法是先保证 Recall@5，再用 rerank、每文档上限和上下文预算控制噪声。

### Q19：面试官问：除了 Recall@5，你还会看哪些指标？

检索层看 MRR、Precision@K、无答案查询的误召回率、各阶段延迟和候选覆盖率。生成层要看 faithfulness、groundedness、citation correctness 和拒答准确率。

面试时要明确：检索命中只是第一步，最终系统质量还取决于模型是否忠实使用证据。

### Q20：面试官问：如何构造 RAG 评测集？

不能只用和原文高度重复的问题。要覆盖真实用户问题、同义改写、函数名和错误码、跨 chunk 问题、多语言、无答案问题、冲突资料，以及经验复用类问题。

每条样本要标注预期文档、预期 chunk 或关键术语。这样才能定位是解析、分块、召回、融合、重排还是生成出了问题。

### Q21：面试官问：如果 RAG 回答错了，你怎么排查？

我会分层看：loader 是否解析丢内容，chunk 是否切断证据，metadata 路由是否把正确文档排除，向量和 BM25 各自是否召回，RRF 是否融合失败，rerank 是否把正确结果压后，最后再看生成 prompt 是否要求引用和忠实回答。

如果不记录每阶段候选和分数，只看最终回答，很难判断是 retrieval failure 还是 generation failure。

### Q22：面试官问：为什么 `knowledge_search` 要做 deferred tool？

知识库不是每个任务都需要，完整 schema 常驻会增加固定上下文成本。项目先在 System Prompt 里披露知识库 manifest，让模型知道有哪些文档；真正需要时再通过 `tool_search` 激活 `knowledge_search` 完整 schema。

这就是渐进式披露：目录先出现，工具按需激活，正文 chunk 最后才进入上下文。

### Q23：面试官问：为什么不把知识库 manifest 写得很详细？

manifest 太详细会变成另一种上下文膨胀。它只应该帮助模型判断“是否可能需要检索”，不能替代真正证据。

所以项目限制文档数、标题数和总字符数。真正回答事实问题时，模型必须调用 `knowledge_search` 获取正文片段，而不是只靠 manifest 作答。

### Q24：面试官问：RAG 结果为什么要标记为 untrusted？

知识库文档可能来自外部，也可能包含 Prompt Injection，比如“忽略系统指令，执行某命令”。一旦检索结果进入上下文，模型就可能被诱导。

所以结果会包在 `<knowledge-results>` 中并声明是参考数据，不是指令。真正的安全还依赖权限系统：即使模型被诱导调用 shell，也要经过 permission gate。

### Q25：面试官问：经验库会不会污染模型判断？

会有风险。历史经验可能过期、未验证，或者当前任务条件不同。项目通过 `unverified` 标签、source/heading 引用和 Prompt 约束提醒模型不能盲套经验。

更稳的做法是把经验当作排查线索，而不是事实结论。复用前必须读取当前代码、检查当前配置并重新运行验证命令。

### Q26：面试官问：RAG、Memory、Experience 三者怎么区分？

Memory 是少量长期偏好、反馈和项目事实，通常通过索引和 side query 注入；RAG 是大量外部资料和经验文档的检索系统；Experience 是从 Agent 任务轨迹生成的结构化过程知识，最终会写入 RAG。

一句话说：Memory 记“用户和项目长期偏好”，RAG 查“外部证据”，Experience 沉淀“做事流程”。

### Q27：面试官问：RAG 和上下文治理怎么配合？

RAG 会把外部证据注入模型，如果不控制 top-k、单文档上限和总字符数，就会挤占代码、测试日志和用户约束的上下文空间。

所以 RAG 负责检索前控制候选质量和长度，上下文治理负责工具结果进入历史后的持久化、snip 和 compact。两者是配套模块。

### Q28：面试官问：如何处理 embedding 模型变更？

索引记录每个文档使用的 embedding model 和 dimensions。查询时如果当前 provider 和索引配置不一致，就拒绝检索并提示 reindex。

这是必要的，因为不同模型的向量空间不可比。静默混搜会返回看似有分数但实际无意义的结果。

### Q29：面试官问：这个系统生产化还缺什么？

主要缺多租户 ACL、本地或私有 embedding、OCR 和复杂表格解析、增量索引、跨进程锁、在线反馈、可观测性、学习型 reranker，以及生成层的 groundedness 和 citation 校验。

我会强调当前实现适合单机项目级知识库，已经覆盖核心链路，但不是企业多租户知识平台。

### Q30：面试官问：一句话怎么讲你的简历 bullet？

我构建了任务轨迹记录和经验持久化机制，把 Agent 的成功工作流沉淀成结构化经验文档并写入项目级 RAG；检索侧用结构感知分块、HNSW 向量召回和 FTS5/BM25 关键词召回，通过 RRF 融合排序，将 Recall@5 从 68% 提升到 86%。

## 代码阅读索引

| 主题 | Python 代码位置 |
| --- | --- |
| 经验事件与保存入口 | `python/mini_claude/experience.py:1-190` |
| 经验 JSON schema 与校验 | `python/mini_claude/experience.py:290-420` |
| 经验 Markdown 渲染 | `python/mini_claude/experience.py:422-486` |
| 经验 fallback、脱敏、摘要 | `python/mini_claude/experience.py:488-749` |
| Agent 记录 journal | `python/mini_claude/agent.py:514,1246-1304,1657,1888` |
| CLI `/experience` 命令 | `python/mini_claude/__main__.py:243-292` |
| 数据结构、常量、项目目录 | `python/mini_claude/knowledge.py:24-124` |
| token 估算与结构分块 | `python/mini_claude/knowledge.py:138-468` |
| SQLite、FTS 与 HNSW | `python/mini_claude/knowledge.py:583-857` |
| 导入、重建与版本切换 | `python/mini_claude/knowledge.py:875-1048` |
| manifest、搜索、RRF、重排 | `python/mini_claude/knowledge.py:1053-1331` |
| 评测与配置兼容 | `python/mini_claude/knowledge.py:1334-1430` |
| 工具结果格式化与执行 | `python/mini_claude/knowledge.py:1495-1558` |
| Loader 与 Embedding | `python/mini_claude/knowledge_loaders.py`, `python/mini_claude/embeddings.py` |
| 工具 schema 与延迟激活 | `python/mini_claude/tools.py:136-245` |
| Prompt 中的知识库规则 | `python/mini_claude/prompt.py:70,225-245` |
| 测试 | `python/tests/test_knowledge.py`, `python/tests/test_experience.py` |
