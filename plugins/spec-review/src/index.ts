import { resolve } from "node:path"
import { tool, type Plugin } from "@opencode-ai/plugin"
import { invokeRuntime } from "./runtime"
import { COMMAND_TEMPLATE, REVIEW_AGENT_PROMPT } from "./prompts"

const strings = tool.schema.array(tool.schema.string())
const optionalStrings = strings.optional()
const repoArgument = tool.schema.string().describe("业务仓库的绝对路径；必须与当前审查案例一致。")

type ToolContext = { worktree?: string; directory?: string }

function resolveRepository(requested: string | undefined, context: ToolContext): string {
  const candidate = requested?.trim() || context.worktree || context.directory || ""
  if (!candidate) {
    throw new Error("无法确定业务仓库。请显式传入 repo 的绝对路径。")
  }
  const repository = resolve(candidate)
  if (repository === "/") {
    throw new Error("拒绝把文件系统根目录 / 作为业务仓库。请显式传入正确的 repo。")
  }
  return repository
}

const SpecReviewPlugin: Plugin = async () => ({
  config: async (config) => {
    const mutable = config as typeof config & {
      command?: Record<string, unknown>
      agent?: Record<string, unknown>
    }
    mutable.command ??= {}
    mutable.command["spec-review"] = {
      description: "调用 spec-review 子 Agent 审查需求与代码变更的一致性",
      agent: "spec-review",
      subtask: true,
      template: COMMAND_TEMPLATE,
    }

    mutable.agent ??= {}
    mutable.agent["spec-review"] = {
      description: "基于需求文档、代码差异、符号表和调用链证据进行一致性审查",
      mode: "subagent",
      hidden: false,
      prompt: REVIEW_AGENT_PROMPT,
      permission: {
        "*": "deny",
        "spec_review_*": "allow",
      },
    }
  },

  tool: {
    spec_review_start: tool({
      description: "创建审查案例、建立增量索引，并返回第一个阶段动作。",
      args: {
        repo: repoArgument.optional().describe("业务仓库绝对路径；省略时使用当前 OpenCode 工作目录。"),
        docs: strings.describe("需求文档路径，可使用相对业务仓库的路径或绝对路径。"),
        base: tool.schema.string().optional().describe("Git 基准版本；用于审查一次代码变更。"),
        head: tool.schema.string().optional().describe("Git 目标版本，默认使用 HEAD。"),
        paths: optionalStrings.describe("代码路径过滤条件。"),
        sections: optionalStrings.describe("需求文档章节标题。"),
        fullRepo: tool.schema.boolean().default(false).describe("明确允许无 base、无 paths 的全仓审查。"),
        mode: tool.schema.enum(["fast", "deep", "auto"]).default("auto"),
      },
      execute: async (args, context) => {
        const repo = resolveRepository(args.repo, context)
        return invokeRuntime("start", { ...args, repo }, repo)
      },
    }),

    spec_review_status: tool({
      description: "返回指定仓库中审查案例的状态和当前动作。",
      args: { repo: repoArgument, caseId: tool.schema.string().optional() },
      execute: async (args) => invokeRuntime("status", args, resolveRepository(args.repo, {})),
    }),

    spec_review_next: tool({
      description: "当前阶段提交成功后，将同一个子 Agent 推进到下一阶段。",
      args: { repo: repoArgument, caseId: tool.schema.string() },
      execute: async (args) => invokeRuntime("next", args, resolveRepository(args.repo, {})),
    }),

    spec_review_context: tool({
      description: "获取有界上下文包，或针对明确的证据缺口沿调用链补充取证。",
      args: {
        repo: repoArgument,
        caseId: tool.schema.string(),
        claimId: tool.schema.string().optional(),
        gapId: tool.schema.string().optional(),
        direction: tool.schema.enum(["both", "callers", "callees"]).default("both"),
        maxNodes: tool.schema.number().int().min(1).max(200).default(40),
      },
      execute: async (args) => invokeRuntime("context", args, resolveRepository(args.repo, {})),
    }),

    spec_review_submit: tool({
      description: "为当前阶段提交一次结构化中文审查结果。",
      args: {
        repo: repoArgument,
        caseId: tool.schema.string(),
        stage: tool.schema.enum([
          "l3_review",
          "l4_initial",
          "l4_challenge",
          "l4_investigate",
          "l4_converge",
        ]),
        result: tool.schema.string().describe("编码为字符串的 JSON 对象；自然语言字段使用中文。"),
      },
      execute: async (args) => invokeRuntime("submit", args, resolveRepository(args.repo, {})),
    }),

    spec_review_finish: tool({
      description: "为已完成的审查案例生成 Markdown 和 JSON 中文报告。",
      args: { repo: repoArgument, caseId: tool.schema.string() },
      execute: async (args) => invokeRuntime("finish", args, resolveRepository(args.repo, {})),
    }),
  },
})

export default SpecReviewPlugin
