import { spawnSync } from "node:child_process";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const mode = process.argv[2] || "check";
if (!["check", "run"].includes(mode)) throw new Error("运行模式只能是 check 或 run。");
const repoRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const windows = process.platform === "win32";
const command = windows ? "wsl.exe" : "bash";
const args = windows
  ? ["--distribution", process.env.SANDCASTLE_WSL_DISTRO || "Ubuntu-24.04", "--cd", repoRoot, "--exec", "bash", ".sandcastle/launch.sh", mode]
  : [resolve(repoRoot, ".sandcastle/launch.sh"), mode];
const result = spawnSync(command, args, { cwd: repoRoot, stdio: "inherit" });
if (result.error) throw result.error;
process.exit(result.status ?? 1);
