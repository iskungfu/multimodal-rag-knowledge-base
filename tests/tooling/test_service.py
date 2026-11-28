from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from scripts import service


@contextmanager
def reserved_port():
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        yield server.getsockname()[1]


def command(action, port, name="query"):
    environment = dict(os.environ, PYTHON_DOTENV_DISABLED="1", HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    result = subprocess.run([sys.executable, str(Path(service.__file__).resolve()), action,
                             name, "--port", str(port), "--no-schedules"], cwd=service.ROOT, env=environment,
                            capture_output=True, text=True, encoding="utf-8", timeout=45)
    return result, json.loads(result.stdout)


def test_unmanaged_listener_cannot_be_started_over_or_stopped(monkeypatch, tmp_path):
    monkeypatch.setattr(service, "STATE", tmp_path)
    with reserved_port() as port:
        assert service.status("query", port)["status"] == "unmanaged_port_in_use"
        with pytest.raises(ValueError):
            service.start("query", port)
        with pytest.raises(ValueError):
            service.stop("query", port)
        assert service.listening(port)


def test_stale_pid_identity_is_rejected():
    assert not service.same_process({"pid": os.getpid(), "create_time": 0, "service": "query", "port": 1})


def test_cleanup_failure_remains_visible_after_process_exit(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(service, "STATE", tmp_path)
    with reserved_port() as port:
        pass
    service.write_state(service.state_path("query", port), {
        "status": "cleanup_incomplete", "cleanup": {"workers_stopped": False}})
    monkeypatch.setattr(sys, "argv", ["service.py", "status", "query", "--port", str(port)])
    assert service.main() == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "stopped" and result["last_status"] == "cleanup_incomplete"
    assert result["cleanup"] == {"workers_stopped": False}


@pytest.mark.parametrize("name", ["query", "import"])
def test_real_service_start_status_idempotence_and_graceful_stop(name):
    with reserved_port() as port:
        pass
    try:
        result, started = command("start", port, name)
        assert result.returncode == 0, started
        assert started["status"] == "running" and started["jobs"] == []
        assert "token" not in started and "control_port" not in started
        result, repeated = command("start", port, name)
        assert result.returncode == 0 and repeated["pid"] == started["pid"]
        result, current = command("status", port, name)
        assert result.returncode == 0 and current["port_listening"]
        assert current["source_unchanged"]
        with urlopen(f"http://127.0.0.1:{port}/openapi.json", timeout=3) as response:
            routes = json.load(response)["paths"]
        assert ("/query" if name == "query" else "/upload") in routes
        record = service.read_state(service.state_path(name, port))
        request = Request(f'http://127.0.0.1:{record["control_port"]}/stop', method="POST")
        with pytest.raises(HTTPError) as denied:
            urlopen(request, timeout=3)
        assert denied.value.code == 403 and service.listening(port)
        result, stopped = command("stop", port, name)
        assert result.returncode == 0 and stopped["status"] == "stopped"
        assert not service.listening(port)
        persisted = service.read_state(service.state_path(name, port))
        assert persisted["status"] == "stopped" and "token" not in persisted
        result, restarted = command("start", port, name)
        assert result.returncode == 0 and restarted["instance"] != started["instance"]
    finally:
        record = service.read_state(service.state_path(name, port))
        if record and service.same_process(record):
            service.stop(name, port)
