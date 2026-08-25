# 知识库演示文件

这组文件用于测试 mini-claude 的多格式知识库、metadata routing、chunking、混合检索和 rerank。文档共同描述一个虚构的“智能面试平台”，内容刻意写得比最小样例更长，便于观察不同文件类型的 chunk 行为。

## 文件用途

- `01-system-design.md`：Markdown frontmatter、多级标题、架构设计、可靠性边界。
- `02-file-upload.html`：HTML 标题保留、忽略 `<head>`、文件上传与解析链路。
- `03-project-modules.csv`：表格行组切片、重复表头、模块职责检索。
- `04-project-config.json`：结构化项目配置、JSONPath 感知切片。
- `05-deployment-runbook.txt`：普通文本、段落递归切片和运维问题。
- `06-decisions.jsonl`：多条架构决策记录、record-aware 切片。
- `eval-cases.json`：检索评测集，不建议导入知识库。

## 导入

在 mini-claude REPL 中逐个执行：

```text
/kb add examples/knowledge-base-demo/01-system-design.md
/kb add examples/knowledge-base-demo/02-file-upload.html
/kb add examples/knowledge-base-demo/03-project-modules.csv
/kb add examples/knowledge-base-demo/04-project-config.json
/kb add examples/knowledge-base-demo/05-deployment-runbook.txt
/kb add examples/knowledge-base-demo/06-decisions.jsonl
```

如果你是在仓库的 `python/` 目录里启动 `mini-claude-py`，路径要加一层 `..`：

```text
/kb add ../examples/knowledge-base-demo/01-system-design.md
```

导入后可直接提问：

```text
文件上传部分包括哪些校验和容错？
为什么原始文件不存在数据库里？
Worker 离线后如何恢复未完成任务？
CSV 和 JSON 文件现在分别怎么切片？
RAG 默认的 chunk token 和 rerank topK 是多少？
```

评测检索效果：

```text
/kb eval examples/knowledge-base-demo/eval-cases.json
```

第一条评测用例使用 `expected_terms_mode: across_hits`，用于验证“MIME 校验”和“SHA-256 去重”分布在不同章节时的多证据召回。
