# 安装

本目录是 `spec-review` 的自包含运行包，但 OpenCode 1.x 还需要同级的
`../spec-review.ts` 直接入口。请从发行包根目录复制完整的 `opencode` 配置覆盖目录，
不要单独复制本目录。

## 全局安装

把发行包根目录中的 `opencode` 文件夹整体合并到 `~/.config/`，最终形成：

```text
~/.config/opencode/plugins/spec-review.ts
~/.config/opencode/plugins/spec-review/
```

最终应能看到：

```text
~/.config/opencode/plugins/spec-review/package.json
~/.config/opencode/plugins/spec-review/index.ts
~/.config/opencode/plugins/spec-review/src/
~/.config/opencode/plugins/spec-review/runtime/
~/.config/opencode/plugins/spec-review/node_modules/
```

然后重启 OpenCode。不需要执行 `npm install`、`bun install` 或 `pip install`。

运行环境需要：

- OpenCode 1.18.0 或更高版本；
- `python3` 3.9 或更高版本可从 PATH 访问；
- 目标代码目录是 Git 仓库。

## 使用

```text
/spec-review --docs docs/design.md --base main --head HEAD --mode auto
```

或者：

```text
@spec-review 审查 HEAD 相对 main 的变更是否符合 docs/design.md
```

插件会在被审查的业务仓库中创建 `.spec-review/`，不会读写 OpenCode 自身数据库。
