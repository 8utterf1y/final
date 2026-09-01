# Spec Review 需求设计文档

## 1. 产品目标

在公司内部 OpenCode 中提供一个名为 `spec-review` 的默认只读子 Agent，用于回答：

> 指定的代码变更是否符合选定需求文档中的相关要求？

工具必须支持 GitHub PR、一次提交、一个差异范围、部分路径或部分需求章节；不能假设每次都
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
       ├─ PR 解析、SHA 锁定与本地 Git diff
       ├─ 需求声明提取
       ├─ Tree-sitter/AST 索引
       ├─ 调用图双向有界 BFS
       ├─ 证据包与稳定 ID
       ├─ SQLite 状态与 Markdown/JSON/SARIF 报告
       └─ 可选的安全发布与建议 Fix PR
```

子 Agent 负责语义判断和审查编排；本地运行时负责路径、范围、索引、状态机、预算、
证据和报告等确定性工作。两者通过结构化 JSON 交互。

## 4. 子 Agent 行为

系统只注册一个公开子 Agent：`spec-review`。它不得再创建其他子 Agent。

### 4.1 L3

按游标分页取得受限上下文，对每条候选需求声明判断 `consistent`、`inconsistent`、
`uncertain` 或 `not_applicable`。默认每页 3 条，每条只携带排序后的 Top-K 相关代码
证据。结果必须逐条覆盖真实 Claim ID；证据不足必须标记 `uncertain`。

### 4.2 L4

1. 初判：形成候选问题与假设。
2. 质疑：主动寻找误报原因与证据缺口。
3. 取证：围绕 gap 按需沿 callers/callees 双向扩展。
4. 收敛：去误报、合并重复、定级、归因并生成最终结论。

每个阶段只允许提交一次。运行时持久化阶段结果并返回下一动作，子 Agent 不自行跳转。
L4 只接收 L3 的 `inconsistent` 和 `uncertain` 候选，不重复处理已定案的 consistent 或
not_applicable。每个阶段提交时，运行时校验 Claim/Evidence 所属关系和完整覆盖率。

## 5. 审查范围

输入包括：仓库绝对路径、需求文档、PR URL、base/head、路径过滤、章节过滤和模式。

- `pr`：从 GitHub API 读取并锁定完整 base/head SHA，在 detached worktree 审查。
- `base + head`：审查指定 Git 差异。
- `paths`：限制到明确代码范围，可与 Git 差异组合。
- `sections`：限制需求文档范围。
- `fullRepo=true`：显式允许无 base、无 paths 的全仓审查。

默认禁止范围不明确的审查。仓库解析结果为 `/` 时，在创建 `.spec-review` 前失败。

## 6. 索引与上下文

- 文件哈希未变化时复用前一快照的符号和边。
- 同一 PR 新 head 会关联前次案例，记录增量文件范围，同时保持完整 PR 结论覆盖。
- 使用 Tree-sitter 查询提取定义和引用；Python 在依赖不可用时使用标准库 AST。
- 解析能力不足必须记录状态，不能把“未索引到”当成“不存在”。
- 调用边包含解析器、置信度和解析状态。
- 从差异命中的符号作为种子，按需执行有节点上限的 callers/callees BFS。
- Snapshot 模式从路径范围内符号按需求相关性选取种子，不生成空 Diff 证据。
- Python AST 同时索引类、函数、方法、模块/类常量与变量，支持阈值和状态常量取证。
- 代码片段、diff 和元数据持久化为稳定 `evidence_id`，模型只能引用这些证据。

## 7. 持久化与并发

所有数据位于业务仓库 `.spec-review/`。每次工具调用启动一个进程并在结束后退出。

- `runtime.lock`：仓库级排他内核锁，避免多个写者竞争。
- `index.sqlite`：WAL 模式，设置有限 busy timeout。
- 锁等待超时返回中文诊断和持有者 PID。
- 异常退出由操作系统释放锁，不以“删除锁文件”判断锁是否有效。
- 数据库记录 schema 版本；更高版本数据库必须拒绝由旧运行时打开。

## 8. GitHub 发布与修复边界

- `publish-preview` 永远只读；真实发布前重新读取 PR 并核对锁定 head SHA。
- PR Review 固定绑定 `commit_id`；行内评论只落在 Diff 的 RIGHT/new 行。
- 发布记录以案例、类型、head 和载荷哈希建立唯一键，重试不重复提交。
- 普通 Token 默认使用 Commit Status；Check Run 仅在显式选择且凭据为 GitHub App 时使用。
- SARIF 始终生成到本地，上传必须显式开启并具备 Code Scanning 权限。
- 建议 Patch 不自动应用；人工确认后只在新分支创建 Fix PR，不改原 PR 分支。

## 9. 安全要求

- 子 Agent 无 Shell、通用读取、搜索、编辑和 task 权限。
- 文档路径必须位于业务仓库内。
- 不读写 `~/.local/share/opencode/opencode.db`。
- 不创建根目录状态，不启动网络监听端口，不下载运行依赖。
- 只有 PR 元数据读取或用户明确发布时访问 GitHub；凭据只从环境读取且不持久化。
- 报告只能写入当前业务仓库的 `.spec-review/reports/`。

## 10. 已知边界

- 当前源码发行版完整支持 Python AST；其他语言要达到同等效果，需要在后续发行物中
  预打包相应 Tree-sitter 原生模块或自包含运行时。
- 仅靠静态调用图不能完整建模反射、动态派发、依赖注入和跨服务调用；这些情况必须
  形成证据缺口并降级为 `uncertain`。
- 当前仓库内索引和案例状态共用一个 SQLite 文件，但已由仓库级锁串行化。后续如果
  需要并行只读查询，可把稳定索引与案例状态拆分为两个数据库。

## 11. 验收标准

- 文件夹投放后，无配置改动即可发现一个 `spec-review` 子 Agent。
- 配置中不再出现五个内部审查 Agent。
- `/`、缺失 repo、范围不明确均在写入业务数据前返回明确错误。
- 同一仓库并发调用不会返回原始 `database is locked`，而是有界等待或友好锁提示。
- L3 可完成并生成 Markdown/JSON/SARIF；auto 的 uncertain 能升级到完整 L4。
- PR 输入锁定完整 base/head SHA，head 漂移会阻断发布，重复发布不会产生重复 Review。
- 建议 Patch 默认不落盘；没有人工确认词时不能创建 Fix PR。
- 大文档必须通过分页完成，任何 UNKNOWN/REMAINING 聚合 Claim 或未覆盖声明均被拒绝。
- 报告只有在全部声明达到终态时为 completed；否则状态为 incomplete。
- 每个最终不一致项包含需求声明、代码证据 ID、严重级别和变更归因。
