# Spec Review：OpenCode 需求—代码一致性审查子 Agent

> 此目录是插件运行包，必须和同级的 `../spec-review.ts` 加载入口一起分发。
> 请从发行包根目录按照总 README 操作，不要单独复制本目录，也不要执行依赖安装命令。

Spec Review 是一个可直接投放到 OpenCode 插件目录的专用子 Agent。它根据选定的中文
需求文档审查指定代码变更，通过本地符号索引和双向调用图生成有界上下文包，再执行
L3 快速审查或 L4“初判—质疑—取证—收敛”深度审查。

它不是 MCP，不启动 Sidecar，不运行常驻服务，也不读写 OpenCode 自己的数据库。

## 目录投放

发行物通过同级的 `plugins/spec-review.ts` 加载本目录。完整发行布局应复制到：

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

重启 OpenCode 后即可使用。不需要执行安装脚本，不需要修改 `opencode.jsonc`，也不需要
运行 `npm install`、`bun install` 或 `pip install`。所需插件接口与参数校验库已随包提供。

OpenCode 当前的本地插件发现规则会扫描 `~/.config/opencode/plugins/`；其中的直接
子目录在具有 `package.json` 的 `exports`、`main` 或 `index.ts` 入口时可作为插件包
加载。本包同时声明了 `exports` 和 `main`。

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

L4 的四个阶段由同一个 `spec-review` 子 Agent 顺序执行，不会继续嵌套创建子 Agent。
这能保留完整的质疑与收敛流程，同时避免上下文重复、阶段失联和任务树失控。

## 本地数据

每个业务仓库只使用自己的状态目录：

```text
<业务仓库>/.spec-review/
├── index.sqlite
├── runtime.lock
└── reports/<case-id>/
    ├── review.md
    └── review.json
```

建议将 `.spec-review/` 加入业务仓库的 `.gitignore`。`runtime.lock` 使用操作系统文件锁；
进程退出后锁自动释放，文件本身保留用于诊断，不需要人工删除。

## 运行时边界

- 仓库路径在首次调用时解析为绝对路径，`/` 会被拒绝。
- 后续工具调用必须同时携带相同的 `repo` 和 `caseId`。
- 同一仓库的写操作通过文件锁串行化，SQLite 同时使用 WAL 和 busy timeout。
- Agent 只能调用 `spec_review_*` 工具，不能用 Shell 或通用文件工具绕过证据运行时。
- 调用图是导航线索；最终结论必须引用具体源码或差异的 `evidence_id`。
- 当前无需额外 Python 包；Python 使用 AST 后端。其他语言在 Tree-sitter 原生语法包
  未随发行物提供时会明确标记 `backend_unavailable`，不会伪称已完成语义索引。

## 发行方验证

下面的命令只供维护者测试，使用者不需要执行：

```bash
python3 -m unittest discover -s runtime/tests -v
```

构建后的整个目录可以压缩后发给同事，也可以由公司软件分发系统直接投放到固定目录。
