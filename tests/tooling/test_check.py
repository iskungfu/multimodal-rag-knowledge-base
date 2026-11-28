"""检查入口失败必须阻止后续步骤，并强制关闭调度/真实集成。"""
from types import SimpleNamespace

from scripts import check


def test_failed_lint_stops_checks_and_forces_offline_environment(monkeypatch):
    calls = []
    monkeypatch.setenv("DAILY_SUMMARY_ENABLED", "1")
    monkeypatch.setenv("MARKET_COLLECTION_ENABLED", "1")
    monkeypatch.setenv("AFK_RUN_SUMMARY_INTEGRATION", "1")
    monkeypatch.setattr(check.shutil, "which", lambda name: "/fake/node")

    def execute(command, **options):
        calls.append((command, options))
        return SimpleNamespace(returncode=7)

    monkeypatch.setattr(check.subprocess, "run", execute)
    assert check.main() == 7
    assert len(calls) == 1
    environment = calls[0][1]["env"]
    assert environment["DAILY_SUMMARY_ENABLED"] == "0"
    assert environment["MARKET_COLLECTION_ENABLED"] == "0"
    assert environment["AFK_RUN_SUMMARY_INTEGRATION"] == "0"
    assert environment["PYTHON_DOTENV_DISABLED"] == "1"
