# Spec Review 需求设计文档

## 1. 产品目标

在公司内部 OpenCode 中提供一个名为 `spec-review` 的只读子 Agent，用于回答：

> 指定的代码变更是否符合选定需求文档中的相关要求？

工具必须支持只审查一次提交、一个差异范围、部分路径或部分需求章节；不能假设每次都
是全仓或 RFC 审查。需求文档按普通、易读的 Markdown 文档处理，以标题、段落和列表
形成候选需求声明。

## 2. 交付与部署要求

- 交付物是单个 `spec-review` 文件夹。
- 投放位置是 `~/.config/opencode/plugins/spec-review/`。
- 重启 OpenCode 后自动出现 `spec-review` 子 Agent 和 `/spec-review` 命令。
- 使用者不执行安装脚本，不修改 OpenCode 配置，不安装运行时依赖。
- 不接入 MCP，不启动 Sidecar，不使用 OpenCode 内部 SQLite。
- 升级通过完整替换该文件夹完成；回退通过换回旧文件夹完成。

## 3. 总体架构

```text
OpenCode 主 Agent 或 /spec-review
              │
              ▼
       spec-review 子 Agent
       ├─ L3 单次快速推理
       └─ L4 单 Agent 四阶段状态机
              │ spec_review_* 工具
              ▼
       短生命周期本地运行时
       ├─ 范围与 Git diff
       ├─ 需求声明提取
       ├─ Tree-sitter/AST 索引
       ├─ 调用图双向有界 BFS
       ├─ 证据包与稳定 ID
       └─ SQLite 状态与报告
```

子 Agent 负责语义判断和审查编排；本地运行时负责路径、范围、索引、状态机、预算、
证据和报告等确定性工作。两者通过结构化 JSON 交互。

## 4. 子 Agent 行为

系统只注册一个公开子 Agent：`spec-review`。它不得再创建其他子 Agent。

### 4.1 L3

一次取得受限上下文，对候选需求声明快速判断，产生 `consistent`、`inconsistent`、
`uncertain` 或 `not_applicable`。结果是初筛清单；证据不足必须标记 `uncertain`。

### 4.2 L4

1. 初判：形成候选问题与假设。
2. 质疑：主动寻找误报原因与证据缺口。
3. 取证：围绕 gap 按需沿 callers/callees 双向扩展。
4. 收敛：去误报、合并重复、定级、归因并生成最终结论。

每个阶段只允许提交一次。运行时持久化阶段结果并返回下一动作，子 Agent 不自行跳转。

## 5. 审查范围

输入包括：仓库绝对路径、需求文档、base/head、路径过滤、章节过滤和模式。

- `base + head`：审查指定 Git 差异。
- `paths`：限制到明确代码范围，可与 Git 差异组合。
- `sections`：限制需求文档范围。
- `fullRepo=true`：显式允许无 base、无 paths 的全仓审查。

默认禁止范围不明确的审查。仓库解析结果为 `/` 时，在创建 `.spec-review` 前失败。

## 6. 索引与上下文

- 文件哈希未变化时复用前一快照的符号和边。
- 使用 Tree-sitter 查询提取定义和引用；Python 在依赖不可用时使用标准库 AST。
- 解析能力不足必须记录状态，不能把“未索引到”当成“不存在”。
- 调用边包含解析器、置信度和解析状态。
- 从差异命中的符号作为种子，按需执行有节点上限的 callers/callees BFS。
- 代码片段、diff 和元数据持久化为稳定 `evidence_id`，模型只能引用这些证据。

## 7. 持久化与并发

所有数据位于业务仓库 `.spec-review/`。每次工具调用启动一个进程并在结束后退出。

- `runtime.lock`：仓库级排他内核锁，避免多个写者竞争。
- `index.sqlite`：WAL 模式，设置有限 busy timeout。
- 锁等待超时返回中文诊断和持有者 PID。
- 异常退出由操作系统释放锁，不以“删除锁文件”判断锁是否有效。
- 数据库记录 schema 版本；更高版本数据库必须拒绝由旧运行时打开。

## 8. 安全要求

- 子 Agent 无 Shell、通用读取、搜索、编辑和 task 权限。
- 文档路径必须位于业务仓库内。
- 不读写 `~/.local/share/opencode/opencode.db`。
- 不创建根目录状态，不启动网络监听端口，不下载依赖。
- 报告只能写入当前业务仓库的 `.spec-review/reports/`。

## 9. 已知边界

- 当前源码发行版完整支持 Python AST；其他语言要达到同等效果，需要在后续发行物中
  预打包相应 Tree-sitter 原生模块或自包含运行时。
- 仅靠静态调用图不能完整建模反射、动态派发、依赖注入和跨服务调用；这些情况必须
  形成证据缺口并降级为 `uncertain`。
- 当前仓库内索引和案例状态共用一个 SQLite 文件，但已由仓库级锁串行化。后续如果
  需要并行只读查询，可把稳定索引与案例状态拆分为两个数据库。

## 10. 验收标准

- 文件夹投放后，无配置改动即可发现一个 `spec-review` 子 Agent。
- 配置中不再出现五个内部审查 Agent。
- `/`、缺失 repo、范围不明确均在写入业务数据前返回明确错误。
- 同一仓库并发调用不会返回原始 `database is locked`，而是有界等待或友好锁提示。
- L3 可完成并生成 Markdown/JSON；auto 的 uncertain 能升级到完整 L4。
- 每个最终不一致项包含需求声明、代码证据 ID、严重级别和变更归因。
