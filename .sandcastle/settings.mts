import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { homedir } from "node:os";
import { loadEnvFile } from "node:process";
import { parseEnv } from "node:util";
import { codex } from "@ai-hero/sandcastle";
import { bwrap } from "@ai-hero/sandcastle/sandboxes/bwrap";

export function loadSettings() {
  if (process.platform !== "linux") throw new Error("请通过 npm scripts 在 WSL2 中运行。");
  const repoRoot = process.cwd();
  loadEnvFile(resolve(repoRoot, ".sandcastle/.env"));
  for (const name of ["GH_TOKEN", "CODEX_API_KEY", "SANDCASTLE_MODEL"]) {
    if (!process.env[name]?.trim()) throw new Error(`缺少配置：${name}`);
  }

  const maxIterations = Number(process.env.SANDCASTLE_MAX_ITERATIONS || "1");
  if (!Number.isSafeInteger(maxIterations) || maxIterations < 1 || maxIterations > 20) {
    throw new Error("SANDCASTLE_MAX_ITERATIONS 必须是 1 到 20 的整数。");
  }

  const env: Record<string, string> = {
    GH_REPO: "iskungfu/multimodal-rag-knowledge-base",
    PIP_INDEX_URL: process.env.PIP_INDEX_URL || "https://mirrors.aliyun.com/pypi/simple",
    PIP_CONFIG_FILE: "/dev/null",
    PIP_DISABLE_PIP_VERSION_CHECK: "1",
  };
  for (const name of ["GH_TOKEN", "CODEX_API_KEY", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"]) {
    if (process.env[name]) env[name] = process.env[name]!;
  }
  const storageKeys = ["MONGO_URL", "MONGO_DB_NAME", "MILVUS_URL", "MINIO_ENDPOINT", "MINIO_ACCESS_KEY", "MINIO_SECRET_KEY", "MINIO_BUCKET_NAME", "CHUNKS_COLLECTION", "ITEM_NAME_COLLECTION"];
  const appConfig = parseEnv(readFileSync(resolve(repoRoot, "knowledge/.env"), "utf8"));
  const storageEnv: Record<string, string> = {};
  for (const name of storageKeys) {
    if (appConfig[name]) storageEnv[name] = appConfig[name];
  }
  Object.assign(env, storageEnv);
  for (const name of ["OPEN_API_BASE", "OPEN_API_KEY", "LLM_DEFAULT_MODEL"]) {
    if (appConfig[name]) env[name] = appConfig[name];
  }
  env.AFK_TEST_DB_NAME = `sandcastle_test_${Date.now().toString(36)}`;
  const storageVenv = resolve(homedir(), ".local/share/shopkeeper-brain/storage-probe");
  const storagePython = resolve(storageVenv, "bin/python");
  env.AFK_STORAGE_PYTHON = storagePython;
  const config = readFileSync(resolve(repoRoot, ".sandcastle/codex.local.toml"), "utf8");
  const encodedConfig = Buffer.from(config).toString("base64");
  const configureCodex = `umask 077; mkdir -p "$HOME/.codex"; printf '%s' '${encodedConfig}' | base64 --decode > "$HOME/.codex/config.toml"`;

  return {
    repoRoot,
    env,
    storageEnv,
    storagePython,
    maxIterations,
    sandbox: bwrap({ env, mounts: [{ hostPath: storageVenv, sandboxPath: storageVenv, readonly: true }] }),
    agent: codex(process.env.SANDCASTLE_MODEL!, { effort: "medium", captureSessions: false }),
    hooks: { sandbox: { onSandboxReady: [{ command: configureCodex }] } },
  };
}
