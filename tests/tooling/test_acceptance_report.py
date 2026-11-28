import json
import ast
from pathlib import Path
import subprocess

from scripts.acceptance_report import begin_report, finish_report


def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/probe.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / ".gitignore").write_text(".env\nlogs/\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "-c", "user.name=Tooling Test", "-c", "user.email=tooling@example.invalid",
                    "commit", "-qm", "fixture"], cwd=tmp_path, check=True)
    return tmp_path


def test_unchanged_dirty_sources_can_be_verified_without_storing_contents(tmp_path):
    root = repository(tmp_path)
    (root / "scripts/new.py").write_text("value = 'private-source-sentinel'\n", encoding="utf-8")
    (root / ".env").write_text("SECRET=private-env-sentinel\n", encoding="utf-8")
    report = begin_report({"passed": True}, root=root)
    finish_report(report, root=root)
    assert report["passed"] is True
    assert report["provenance"]["source_unchanged"] is True
    assert "scripts/new.py" in report["provenance"]["start"]["files"]
    assert "private-source-sentinel" not in json.dumps(report)
    assert "private-env-sentinel" not in json.dumps(report)


def test_code_change_invalidates_full_acceptance_but_retains_checks(tmp_path):
    root = repository(tmp_path)
    report = begin_report({"passed": True, "full_acceptance": True, "personal_mvp_acceptance": True,
                           "checks": {"scenario": True}}, root=root)
    (root / "scripts/probe.py").write_text("value = 2\n", encoding="utf-8")
    finish_report(report, root=root)
    assert report["passed"] is False
    assert report["full_acceptance"] is False
    assert report["personal_mvp_acceptance"] is False
    assert report["checks"] == {"scenario": True}
    assert report["provenance"]["changed_files"] == ["scripts/probe.py"]


def test_new_code_is_detected_but_generated_reports_do_not_invalidate_run(tmp_path):
    root = repository(tmp_path)
    report = begin_report({"passed": True}, root=root)
    (root / ".sandcastle/results").mkdir(parents=True)
    (root / ".sandcastle/results/report.md").write_text("run notes", encoding="utf-8")
    finish_report(report, root=root)
    assert report["passed"] is True
    report = begin_report({"passed": True}, root=root)
    (root / "scripts/new.py").write_text("pass\n", encoding="utf-8")
    finish_report(report, root=root)
    assert report["passed"] is False
    assert report["provenance"]["changed_files"] == ["scripts/new.py"]


def test_missing_git_metadata_cannot_claim_verified_version(tmp_path):
    report = begin_report({"passed": True}, root=tmp_path)
    finish_report(report, root=tmp_path)
    assert report["passed"] is False
    assert report["provenance"]["source_unchanged"] is False


def test_changes_during_imports_do_not_replace_the_entry_version(tmp_path):
    root = repository(tmp_path)
    entry = begin_report({}, root=root)
    (root / "scripts/probe.py").write_text("value = 2\n", encoding="utf-8")
    report = begin_report({"passed": True}, root=root, entry_report=entry)
    finish_report(report, root=root)
    assert report["passed"] is False
    assert report["provenance"]["start"] == entry["provenance"]["start"]
    assert report["provenance"]["error_code"] == "source_changed"


def test_every_acceptance_entry_records_source_at_start_and_finish():
    root = Path(__file__).resolve().parents[2]
    for script in (root / "scripts").glob("verify_*.py"):
        tree = ast.parse(script.read_text(encoding="utf-8"))
        main = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "main")
        calls = {node.func.id for node in ast.walk(main) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
        assert {"begin_report", "finish_report"} <= calls, script.name
        entry_index = next(index for index, node in enumerate(tree.body) if isinstance(node, ast.Assign)
                           and any(isinstance(target, ast.Name) and target.id == "ENTRY_REPORT" for target in node.targets))
        local_imports = [index for index, node in enumerate(tree.body) if isinstance(node, ast.ImportFrom)
                         and (node.module or "").startswith("knowledge")]
        assert all(entry_index < index for index in local_imports), script.name
        begin = next(node for node in ast.walk(main) if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Name) and node.func.id == "begin_report")
        assert any(keyword.arg == "entry_report" and isinstance(keyword.value, ast.Name)
                   and keyword.value.id == "ENTRY_REPORT" for keyword in begin.keywords), script.name
