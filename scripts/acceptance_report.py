"""验收报告的代码来源：只保存散列，不保存源码、凭据或模型原文。"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIRS = {"knowledge", "scripts", "tests", ".github"}
SOURCE_FILES = {"package.json", "package-lock.json", "ruff.toml", "pyproject.toml", ".gitignore"}


def source_snapshot(root=ROOT):
    root = Path(root)
    try:
        def git(*args):
            return subprocess.check_output(["git", *args], cwd=root, stderr=subprocess.DEVNULL, timeout=15)

        commit = git("rev-parse", "HEAD").decode().strip()
        names = git("ls-files", "--cached", "--others", "--exclude-standard", "-z").decode("utf-8").split("\0")
        files = {}
        for name in sorted(set(names) - {""}):
            path = Path(name)
            if path.parts[0] not in SOURCE_DIRS and name not in SOURCE_FILES and not name.startswith("requirements"):
                continue
            # 即使意外跟踪了本地配置，验收清单也不读取它。
            if path.name == ".env" or path.name.startswith(".env.") or "__pycache__" in path.parts:
                continue
            source = root / path
            if source.is_symlink():
                raise ValueError("source_symlink")
            files[name] = hashlib.sha256(source.read_bytes()).hexdigest() if source.is_file() else None
        digest = hashlib.sha256(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return {"commit": commit, "source_sha256": digest, "files": files}
    except (OSError, ValueError, subprocess.SubprocessError):
        return {"error_code": "source_snapshot_unavailable"}


def begin_report(report, *, root=ROOT, entry_report=None):
    report["provenance"] = (dict(entry_report["provenance"]) if entry_report is not None else
                            {"schema_version": 1, "started_at": datetime.now(timezone.utc).isoformat(),
                             "python": sys.version.split()[0], "start": source_snapshot(root)})
    return report


def finish_report(report, *, root=ROOT):
    provenance = report.setdefault("provenance", {})
    before, after = provenance.get("start", {}), source_snapshot(root)
    changed = sorted(name for name in before.get("files", {}).keys() | after.get("files", {}).keys()
                     if before.get("files", {}).get(name) != after.get("files", {}).get(name))
    unchanged = ("error_code" not in before and "error_code" not in after
                 and bool(before.get("source_sha256")) and before.get("commit") == after.get("commit")
                 and before.get("source_sha256") == after.get("source_sha256"))
    provenance.update(finished_at=datetime.now(timezone.utc).isoformat(), end=after,
                      source_unchanged=unchanged, changed_files=changed)
    if not unchanged:
        provenance["error_code"] = ("source_changed" if "error_code" not in before and "error_code" not in after and before
                                    else "source_snapshot_unavailable")
        for flag in ("passed", "full_acceptance", "personal_mvp_acceptance"):
            if flag in report:
                report[flag] = False
    return report
