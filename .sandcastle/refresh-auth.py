import json
import os
from pathlib import Path
import subprocess
import tomllib


def capture(arguments, cwd):
    return subprocess.check_output(arguments, cwd=cwd, text=True).strip()


def main():
    repo = Path(__file__).resolve().parent.parent
    codex_root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    config = tomllib.loads((codex_root / "config.toml").read_text(encoding="utf-8-sig"))
    provider = config.get("model_provider", "openai")
    provider_config = config.get("model_providers", {}).get(provider, {})
    key = provider_config.get("experimental_bearer_token") or os.environ.get(provider_config.get("env_key", "CODEX_API_KEY"))
    if not key or not provider_config.get("base_url") or not config.get("model"):
        raise SystemExit("未找到可复用的自定义 Codex provider、模型和凭据；未修改文件。")
    token = capture(["gh", "auth", "token", "--hostname", "github.com"], repo)
    author_name = capture(["git", "config", "user.name"], repo)
    author_email = capture(["git", "config", "user.email"], repo)
    if not token or not author_name or not author_email:
        raise SystemExit("缺少 GitHub 登录或 Git 提交身份；未修改文件。")

    values = {
        "GH_TOKEN": token,
        "CODEX_API_KEY": key,
        "SANDCASTLE_MODEL": config["model"],
        "GIT_AUTHOR_NAME": author_name,
        "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name,
        "GIT_COMMITTER_EMAIL": author_email,
    }
    env_path = repo / ".sandcastle" / ".env"
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.exists() else []
    lines = [line for line in lines if line.partition("=")[0] not in values]
    if not any(line.startswith("SANDCASTLE_MAX_ITERATIONS=") for line in lines):
        lines.append("SANDCASTLE_MAX_ITERATIONS=1")
    lines.extend(f"{name}={json.dumps(value, ensure_ascii=False)}" for name, value in values.items())
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)

    local_config = [
        f"model = {json.dumps(config['model'])}",
        'model_provider = "sandcastle_gateway"',
        "",
        "[model_providers.sandcastle_gateway]",
        'name = "Sandcastle gateway"',
        f"base_url = {json.dumps(provider_config['base_url'])}",
        'wire_api = "responses"',
        'env_key = "CODEX_API_KEY"',
        "requires_openai_auth = false",
    ]
    (repo / ".sandcastle" / "codex.local.toml").write_text("\n".join(local_config) + "\n", encoding="utf-8")
    print("已更新本机凭据和 Codex provider 配置，未输出密钥。")


if __name__ == "__main__":
    main()
