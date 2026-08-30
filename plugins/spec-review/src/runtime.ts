import { dirname, join } from "node:path"
import { fileURLToPath } from "node:url"

const PACKAGE_ROOT = dirname(dirname(fileURLToPath(import.meta.url)))

export type RuntimePayload = Record<string, unknown>

export async function invokeRuntime(
  operation: string,
  payload: RuntimePayload,
  worktree: string,
): Promise<string> {
  const override = process.env.SPEC_REVIEW_RUNTIME
  const command = override
    ? [override, operation, "--payload", "-"]
    : [
        process.env.SPEC_REVIEW_PYTHON || "python3",
        join(PACKAGE_ROOT, "runtime", "spec_review_cli.py"),
        operation,
        "--payload",
        "-",
      ]

  const child = Bun.spawn(command, {
    cwd: worktree && worktree !== "/" ? worktree : undefined,
    stdin: "pipe",
    stdout: "pipe",
    stderr: "pipe",
    env: { ...process.env, PYTHONUTF8: "1" },
  })
  child.stdin.write(JSON.stringify(payload))
  child.stdin.end()

  const [stdout, stderr, exitCode] = await Promise.all([
    new Response(child.stdout).text(),
    new Response(child.stderr).text(),
    child.exited,
  ])
  if (exitCode !== 0) {
    throw new Error(
      `spec-review 运行时执行失败（退出码 ${exitCode}）：${stderr.trim() || stdout.trim()}`,
    )
  }
  return stdout.trim()
}
