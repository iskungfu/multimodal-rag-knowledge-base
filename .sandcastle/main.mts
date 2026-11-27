import { execFileSync } from "node:child_process";
import { resolve } from "node:path";
import { run } from "@ai-hero/sandcastle";
import { loadSettings } from "./settings.mts";

const settings = loadSettings();
const changes = execFileSync("git", ["status", "--porcelain"], {
  cwd: settings.repoRoot,
  encoding: "utf8",
});

if (changes.trim()) {
  throw new Error("当前仓库有未提交改动。请先保存为已提交的基线，再启动 AFK；环境检查可独立运行。");
}

const branch = `codex/sandcastle-${new Date().toISOString().replace(/[:.]/g, "-")}`;
console.log(`本次最多处理 ${settings.maxIterations} 个 Issue，结果保留在 ${branch}。`);

const result = await run({
  name: "investment-assistant",
  cwd: settings.repoRoot,
  sandbox: settings.sandbox,
  agent: settings.agent,
  promptFile: resolve(settings.repoRoot, ".sandcastle/prompt.md"),
  maxIterations: settings.maxIterations,
  branchStrategy: { type: "branch", branch },
  hooks: settings.hooks,
  idleTimeoutSeconds: 600,
});

console.log(`执行结束，分支：${result.branch}`);
console.log(`完成的 agent 调用：${result.iterations.length}，新增 commit：${result.commits.length}`);
if (result.logFilePath) console.log(`日志：${result.logFilePath}`);
if (result.preservedWorktreePath) console.log(`保留的工作目录：${result.preservedWorktreePath}`);
