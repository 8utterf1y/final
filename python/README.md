# Mini Claude Code — Python 版

与 TypeScript 版功能 99% 一致的 Python 实现。**需要 Python >= 3.11**。

> 📖 完整教程文档见 [claude-code-from-scratch](https://github.com/Windy3f3f3f3f/claude-code-from-scratch)（文档中所有代码块均支持 TypeScript / Python 切换）

## 快速开始

```bash
# 安装（需要 Python 3.11+）
cd python
pip install -e .

# 复制项目根目录的环境变量模板
cd ..
cp .env.example .env

# 编辑 .env，填写你的 API Key
# 然后运行
cd python
mini-claude-py "hello"                # 一次性模式
mini-claude-py                        # 交互式 REPL
mini-claude-py --yolo "list files"    # 跳过确认
mini-claude-py --plan "refactor this" # 计划模式
python -m mini_claude "hello"         # 也可以用 python -m 方式运行

# 也可以临时覆盖 .env 中的值
MINI_CLAUDE_MODEL=gpt-4o mini-claude-py "hello"
```

`mini-claude` 会在启动时自动向上查找最近的项目根目录 `.env` 并加载它；如果某个变量已经在当前 shell 里 `export` 过，则 shell 中的值优先，不会被 `.env` 覆盖。

如果你想走 OpenAI 兼容接口，也可以直接写进 `.env`：

```env
OPENAI_API_KEY=sk-xxx
OPENAI_BASE_URL=https://api.openai.com/v1
MINI_CLAUDE_MODEL=gpt-4o
```

## 本地知识库 RAG

知识库按项目隔离保存在 `~/.mini-claude/projects/<project-hash>/knowledge/`。内置支持 Markdown、TXT、HTML、CSV、JSON、JSONL、XML；PDF、DOCX、DOC、PPTX、XLSX、XLS、RTF 通过可选 RAG 解析依赖处理：

```bash
pip install -e '.[rag]'
```

解析链路参考 `interview-guide` 的设计：先把上传文件解析成干净文本，再做切片、embedding 和检索。Python 版用轻量 loader 层承接不同文件格式；复杂 Office/PDF 文档优先交给 MarkItDown 转成 Markdown 后进入现有 heading chunking。

索引使用 OpenAI-compatible Embedding API，可与 Agent 的聊天模型分开配置。推荐也放进项目根目录 `.env`：

```env
MINI_KB_EMBEDDING_API_KEY=sk-...
MINI_KB_EMBEDDING_BASE_URL=https://api.openai.com/v1
MINI_KB_EMBEDDING_MODEL=text-embedding-3-small
# 只在 Provider 支持指定维度时设置
MINI_KB_EMBEDDING_DIMENSIONS=1536
MINI_KB_EMBEDDING_BATCH_SIZE=32

# 可选：切片 token 预算；修改后需执行 /kb reindex all
MINI_KB_CHUNK_TARGET_TOKENS=500
MINI_KB_CHUNK_OVERLAP_TOKENS=60

# 向量索引：auto / exact / hnsw
# auto 在 hnswlib 可用且有效 chunk 达到阈值时启用 HNSW，否则精确检索
MINI_KB_VECTOR_INDEX=auto
MINI_KB_HNSW_MIN_CHUNKS=5000
MINI_KB_HNSW_M=16
MINI_KB_HNSW_EF_CONSTRUCTION=200
MINI_KB_HNSW_EF_SEARCH=64
```

向量检索保留两条路径：小规模或带文档范围过滤的查询使用精确余弦检索；大规模全库查询使用持久化 HNSW 索引先召回候选，再与 FTS5 结果融合。HNSW 索引按当前 chunk 集合、Embedding 模型和维度生成版本指纹；索引缺失、损坏或依赖不可用时，`auto` 模式会安全回退到精确检索。

REPL 命令：

```text
/kb add <path>        导入并建立索引
/kb list              列出当前项目的知识文档
/kb search <query>    调试混合检索结果
/kb remove <id>       删除文档和索引
/kb reindex <id|all>  使用当前 Embedding 配置重建索引
/kb eval <path>       使用 JSON golden set 评估检索质量
```

Agent 通过延迟加载的 `knowledge_search` 工具按需检索，不会把全部文档注入 system prompt。

启动时会把知识库的轻量索引注入 system prompt：只包含文档 ID、标题、来源文件、MIME、parser、chunk 数、标签、章节号和少量章节标题，不包含正文。这样 Agent 能知道“有哪些外部知识源可查”，但回答仍必须通过 `knowledge_search` 读取证据。

导入时会保存文档级 `title/source_dir/mime_type/parser/description/tags/chapter_numbers`，以及 chunk 级 `heading/page_number/chapter_number/tags/token_count`。Markdown/HTML 按标题切分，PDF 在解析结果存在页边界时保留页码，CSV/JSON 按行或结构块组合，所有格式最终受估算 token 预算约束。

检索管道为：

```text
高置信 metadata routing（可选缩小文档范围）
-> HNSW ANN / 精确余弦 top 20 + FTS5 top 20
-> RRF 融合
-> 本地二阶段 rerank（向量、词项覆盖、标题/标签匹配、精确短语）
-> top 3-6 证据块
```

`knowledge_search` 也可显式传入 `document_ids/source_dirs/mime_types/parsers/tags/chapter_numbers`。显式过滤优先于自动路由；自动路由只在元数据命中足够明确时收窄范围，其他情况保留全库召回。

从旧版升级后数据库会自动补列；执行一次 `/kb reindex all` 才会为旧文档回填新增元数据和 token-aware chunks。

评估文件示例：

```json
{
  "cases": [
    {
      "query": "文件上传部分包括什么",
      "expected_source_contains": "README.md",
      "expected_heading_contains": "04. 文件上传",
      "expected_terms": ["Tika", "S3"],
      "expected_terms_mode": "across_hits",
      "top_k": 5
    }
  ]
}
```

`expected_terms_mode` 默认为 `single_hit`，要求所有 `expected_terms` 出现在同一个 chunk。设为 `across_hits` 时，允许同一文档的多个命中 chunk 联合覆盖这些词，rank 是证据首次完整覆盖时的结果位置。

`/kb eval` 会输出 `recall@k`、`mrr`、`precision@k` 和每条 case 的命中 rank。用它对比不同 chunk 大小、overlap、embedding 模型、metadata 过滤和 rerank 策略，避免只凭感觉调参。

## 文件结构

| Python 文件 | 对应 TypeScript | 说明 |
|-------------|----------------|------|
| `agent.py` | `agent.ts` | Agent 核心循环、双后端、4 层压缩 |
| `tools.py` | `tools.ts` | 10 个工具 + 5 种权限模式 |
| `__main__.py` | `cli.ts` | CLI 入口与 REPL |
| `ui.py` | `ui.ts` | 终端 UI（rich） |
| `prompt.py` | `prompt.ts` | 系统提示词构造 |
| `session.py` | `session.ts` | 会话管理 |
| `memory.py` | `memory.ts` | 记忆系统 |
| `knowledge.py` | — | 本地知识库、原子索引与混合检索 |
| `knowledge_loaders.py` | — | 多类型知识文件解析 |
| `embeddings.py` | — | OpenAI-compatible Embedding 适配器 |
| `skills.py` | `skills.ts` | 技能系统 |
| `subagent.py` | `subagent.ts` | 子 Agent |
| `frontmatter.py` | `frontmatter.ts` | YAML frontmatter 解析 |

## 依赖

- `anthropic` — Anthropic SDK（流式）
- `openai` — OpenAI SDK（兼容后端）
- `rich` — 终端彩色输出
- `markitdown` — 可选 RAG 解析依赖，用于 PDF / Office / RTF 等复杂文件
- `hnswlib` / `numpy` — 可选 HNSW 向量索引与数组计算依赖
