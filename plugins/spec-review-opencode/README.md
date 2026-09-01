# Spec Review：OpenCode 需求—代码一致性审查子 Agent

Spec Review 是一个可封装投放到 OpenCode 插件目录的专用子 Agent。它根据选定的中文
需求文档审查指定代码变更。它可直接接收 GitHub PR 链接，锁定 base/head Commit，在
本地构建 Git Diff、AST 与双向调用链证据，再通过确定性状态机执行 L3 快速审查或
L4“初判—质疑—取证—收敛”深度审查。结论可预览后以 PR Review、行内评论、Commit
Status/Check Run 和 SARIF 回流研发工作流。

它不是 MCP，不启动 Sidecar，不运行常驻服务，也不读写 OpenCode 自己的数据库。

项目描述：支持直接接收 GitHub PR 链接，锁定 base/head Commit 并在本地构建 Git Diff、
AST 与双向调用链证据；通过确定性状态机完成多阶段一致性审查，并将可追溯结论以 PR
Review 和检查报告形式回流研发工作流。

## 目录投放

本目录是开发源码，不应直接复制到全局配置。OpenCode 1.18.x 的发行物需要一个直接
`.ts` 加载入口和一个同级自包含运行包，最终布局为：

```text
~/.config/opencode/plugins/spec-review.ts
~/.config/opencode/plugins/spec-review/
```

目录至少应包含：

```text
spec-review/
├── package.json
├── index.ts
├── src/
├── node_modules/                 # 随包提供的最小运行依赖
│   ├── @opencode-ai/plugin/
│   └── zod/
├── runtime/
│   ├── spec_review_cli.py
│   ├── spec_review_runtime/
│   └── queries/
├── README.md
└── DESIGN.md
```

请使用项目生成的 `release/spec-review-global/opencode` 配置覆盖目录进行手动合并。
重启 OpenCode 后即可使用。不需要执行安装脚本，不需要修改 `opencode.jsonc`，也不需要
运行 `npm install`、`bun install` 或 `pip install`。所需插件接口与参数校验库已随包提供。

OpenCode 1.18.x 会扫描 `~/.config/opencode/plugins/` 下的直接 `.ts/.js` 文件，因此
`spec-review.ts` 负责加载同级的 `spec-review/index.ts`；运行包不会依赖外部软链接。

## 如何找到并使用

重启后，Agent 列表中应出现：

```text
spec-review
```

可以让主 Agent 调用它：

```text
使用 spec-review 子 Agent，审查 HEAD 相对 main 的变更是否符合
docs/payment-requirements.md，使用 auto 模式。
```

也可以使用命令入口：

```text
/spec-review --docs docs/payment-requirements.md --base main --head HEAD --mode auto
```

直接审查 GitHub PR（`base/head` 由运行时锁定，不能同时手工指定）：

```text
/spec-review --docs docs/payment-requirements.md \
  --pr https://github.com/acme/payment-service/pull/123 \
  --mode auto
```

运行时会验证本地 remote 属于 PR 的基准仓库，按需 fetch 两个 Commit，并在
`.spec-review/worktrees/<head-sha>` 建立 detached 分析 worktree，不切换或修改当前业务
工作区。再次审查同一 PR 的新 head 时，会按文件哈希复用索引，并记录前后 head 的增量
文件范围；最终结论仍覆盖完整 PR Diff。

只审查部分代码和需求章节：

```text
/spec-review --docs docs/payment-requirements.md \
  --section "重试策略" \
  --base main \
  --path "src/payment/**" \
  --mode deep
```

没有 `base` 时，必须提供至少一个 `path`；只有明确需要全仓审查时才使用
`--full-repo`。这样可以避免误把错误工作目录或过大的仓库作为默认审查范围。

## 审查模式

| 模式 | 行为 |
|---|---|
| `fast` | 仅执行一次 L3 快速审查，输出候选不一致清单 |
| `deep` | 直接执行 L4 四阶段深审 |
| `auto` | 先执行 L3；出现不一致或证据不确定时升级到 L4 |

需求较多时，Agent 会以游标分页处理（默认每页 3 条），不会一次把完整文档与全仓证据
塞入模型上下文。L3 必须逐条覆盖全部真实 Claim ID；L4 只深审 inconsistent/uncertain
候选。运行时拒绝虚构 Claim、跨声明证据和覆盖不足的阶段提交。

L4 的四个阶段由同一个 `spec-review` 子 Agent 顺序执行，不会继续嵌套创建子 Agent。
这能保留完整的质疑与收敛流程，同时避免上下文重复、阶段失联和任务树失控。

## GitHub 安全回写

默认行为始终是“不回写”。审查完成后先使用 `spec_review_publish_preview` 查看将要提交的
Review Summary、可定位到 Diff 新行的评论、检查结论与锁定 SHA。真实发布必须显式调用
`spec_review_publish`，传入预览中的完整 `expectedHeadSha`，并设置 `dryRun=false`；运行时
会再次读取 PR，head 有任何变化都会拒绝发布。同一案例、head 和载荷的重试是幂等的。

- 默认 Review 事件为 `COMMENT`，只有显式选择才使用 `REQUEST_CHANGES`。
- `commit-status` 适用于普通 Token；`check-run` 需要 GitHub App 的 Checks 写权限。
- SARIF 上传默认关闭，显式开启时还需要 Code Scanning/Security Events 权限。
- 真实写操作从环境变量 `GITHUB_TOKEN` 读取凭据，Token 不写入案例数据库或 git 参数。

L4 可生成 `suggested_patch`，但不会应用。`spec_review_fix_preview` 只执行路径、安全大小和
`git apply --check` 校验。只有人工明确要求、提供锁定 SHA 和确认词 `CREATE_FIX_PR` 后，
`spec_review_create_fix_pr` 才会在新 worktree/新分支应用 Patch 并创建独立 Fix PR；原 PR
分支和当前业务工作区保持不变。fork PR 当前只输出建议 Patch。

## 本地数据

每个业务仓库只使用自己的状态目录：

```text
<业务仓库>/.spec-review/
├── index.sqlite
├── runtime.lock
├── worktrees/<head-sha>/          # PR 锁定 Commit 的 detached 分析目录
├── fix-worktrees/<case-id>/       # 仅在人工确认创建 Fix PR 后出现
└── reports/<case-id>/
    ├── review.md
    ├── review.json
    └── review.sarif
```

建议将 `.spec-review/` 加入业务仓库的 `.gitignore`。`runtime.lock` 使用操作系统文件锁；
进程退出后锁自动释放，文件本身保留用于诊断，不需要人工删除。

## 运行时边界

- 仓库路径在首次调用时解析为绝对路径，`/` 会被拒绝。
- 后续工具调用必须同时携带相同的 `repo` 和 `caseId`。
- 同一仓库的写操作通过文件锁串行化，SQLite 同时使用 WAL 和 busy timeout。
- Agent 只能调用 `spec_review_*` 工具，不能用 Shell 或通用文件工具绕过证据运行时。
- PR 模式只访问 GitHub API 和本地配置的 git remote；非 PR 审查保持纯本地。
- 调用图是导航线索；最终结论必须引用具体源码或差异的 `evidence_id`。
- 无 base 时进入 `snapshot` 模式：仍判断当前实现是否满足需求，但归因为
  `unattributed`；有 base 时进入 `comparison` 模式并支持变更归因。
- Snapshot 模式不会生成空 Diff，每条需求只返回排序后的相关源码证据；可使用 query
  对单条需求补充常量、方法或调用链取证。
- 当前无需额外 Python 包；Python 使用 AST 后端。其他语言在 Tree-sitter 原生语法包
  未随发行物提供时会明确标记 `backend_unavailable`，不会伪称已完成语义索引。

## 发行方验证

下面的命令只供维护者测试，使用者不需要执行：

```bash
python3 -m unittest discover -s runtime/tests -v
```

构建后的整个目录可以压缩后发给同事，也可以由公司软件分发系统直接投放到固定目录。
