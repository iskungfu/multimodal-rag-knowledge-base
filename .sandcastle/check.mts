import assert from "node:assert/strict";
import { execFileSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { run } from "@ai-hero/sandcastle";
import { loadSettings } from "./settings.mts";

const settings = loadSettings();
const storageProbe = readFileSync(join(settings.repoRoot, ".sandcastle/check-storage.py")).toString("base64");
const directory = mkdtempSync(join(tmpdir(), "shopkeeper-sandcastle-check-"));
const checkout = join(directory, "repo");
const git = (args: string[]) => execFileSync("git", args, { cwd: checkout, encoding: "utf8" }).trim();
const originalStatus = execFileSync("git", ["status", "--porcelain"], { cwd: settings.repoRoot, encoding: "utf8" });

try {
  execFileSync("git", ["clone", "--quiet", "--no-hardlinks", settings.repoRoot, checkout], { stdio: "pipe" });
  const originalHead = git(["rev-parse", "HEAD"]);
  const modelAgent = settings.agent;
  const smokeAgent = {
    ...modelAgent,
    buildPrintCommand(options: Parameters<typeof modelAgent.buildPrintCommand>[0]) {
      const command = modelAgent.buildPrintCommand(options);
      return { ...command, command: `env -u GH_TOKEN ${command.command}` };
    },
  };

  const result = await run({
    name: "environment-check",
    cwd: checkout,
    sandbox: settings.sandbox,
    agent: smokeAgent,
    branchStrategy: { type: "head" },
    prompt: "这是环境连通性检查。不要调用工具，不要读取或修改文件，不要访问 GitHub。仅输出 SANDCASTLE_OK。",
    maxIterations: 1,
    idleTimeoutSeconds: 120,
    signal: AbortSignal.timeout(180_000),
    hooks: {
      sandbox: {
        onSandboxReady: [
          ...settings.hooks.sandbox.onSandboxReady,
          { command: "test \"$(id -u)\" != 0 && test \"$HOME\" = /home/agent && test ! -e /home/fu/.ssh && test ! -e /mnt/c/Users && node --version && codex --version && python3 --version" },
          { command: "gh api repos/iskungfu/multimodal-rag-knowledge-base --jq .full_name && gh issue list --repo iskungfu/multimodal-rag-knowledge-base --state open --label ready-for-agent --limit 100 --json number --jq '[.[] | select(.number != 1)] | length' && gh api repos/iskungfu/multimodal-rag-knowledge-base/issues/5 --jq .issue_dependencies_summary.blocked_by" },
          { command: `'${settings.storagePython.replace(/'/g, "'\\''")}' -c \"import base64; exec(compile(base64.b64decode('${storageProbe}'), 'check-storage.py', 'exec'))\"` },
        ],
      },
    },
  });

  assert(result.stdout.includes("SANDCASTLE_OK"), "模型没有返回预期的连通性标记。");
  assert.equal(git(["rev-parse", "HEAD"]), originalHead, "检查期间出现了意外 commit。");
  const unexpectedChanges = git(["status", "--porcelain", "--untracked-files=all"])
    .split("\n").filter((line) => line && !line.includes(".sandcastle/logs/"));
  assert.deepEqual(unexpectedChanges, [], "检查期间出现了意外文件修改。");
  assert.equal(execFileSync("git", ["status", "--porcelain"], { cwd: settings.repoRoot, encoding: "utf8" }), originalStatus);
  console.log("检查通过：bwrap 隔离、Linux 工具、GitHub 读取、数据库认证读取、Codex 模型调用。");
  if (originalStatus.trim()) console.log("AFK 尚未启动：当前仓库有未提交改动，需要先准备已提交的基线。");
} finally {
  rmSync(directory, { recursive: true, force: true });
}
