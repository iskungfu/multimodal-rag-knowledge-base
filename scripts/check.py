"""统一离线检查：确定性 Python lint、浏览器 JS 语法及无需真实服务的回归。"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def main():
    environment = dict(os.environ)
    environment.update(DAILY_SUMMARY_ENABLED="0", MARKET_COLLECTION_ENABLED="0",
                       PYTHONDONTWRITEBYTECODE="1", PYTHON_DOTENV_DISABLED="1",
                       HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", PYTHONIOENCODING="utf-8")
    for key in environment:
        if key.startswith("AFK_RUN_"):
            environment[key] = "0"
    environment.update({key: "0" for key in ("AFK_RUN_MARKET_INTEGRATION", "AFK_RUN_REVIEW_INTEGRATION",
                                              "AFK_RUN_RISK_INTEGRATION", "AFK_RUN_SUMMARY_INTEGRATION")})
    node = shutil.which("node")
    if node is None:
        print("需要 Node.js 执行前端语法检查", file=sys.stderr)
        return 1
    commands = [[sys.executable, "-m", "ruff", "check", "knowledge", "scripts", "tests"],
                ["git", "diff", "--check"], ["git", "diff", "--cached", "--check"]]
    commands.extend([node, "--check", str(path)] for path in sorted((ROOT / "knowledge/front/assets/js").glob("*.js")))
    commands.append([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "--tb=short",
                     "knowledge/test/market", "knowledge/test/review", "knowledge/test/strategy",
                     "knowledge/test/risk", "knowledge/test/summary", "knowledge/test/investment",
                     "knowledge/test/query/test_rag_regressions.py", "knowledge/test/test_runtime.py",
                     "tests/tooling"])
    for command in commands:
        print("check:", " ".join(command[1:]), flush=True)
        result = subprocess.run(command, cwd=ROOT, env=environment)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
