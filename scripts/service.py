"""本机服务 start/status/stop；仅管理本脚本启动并验证身份的进程。"""
import argparse
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from urllib.request import Request, urlopen

import psutil

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
STATE = ROOT / ".sandcastle/logs/services"


def state_path(service, port):
    return STATE / f"{service}-{port}.json"


@contextmanager
def launch_lock(path):
    # 锁随父进程退出释放；保留空锁文件不会阻止下次启动。
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


def read_state(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_state(path, record):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def same_process(record):
    try:
        process = psutil.Process(record["pid"])
        arguments = process.cmdline()
        return (abs(process.create_time() - record["create_time"]) < 0.01
                and str(Path(__file__).resolve()) in arguments and "_run" in arguments
                and str(record["port"]) in arguments and record["service"] in arguments
                and Path(process.cwd()).resolve() == ROOT)
    except (psutil.Error, KeyError, TypeError, ValueError):
        return False


def control(record, action):
    if not same_process(record):
        raise ValueError("service_identity_mismatch")
    request = Request(f'http://127.0.0.1:{record["control_port"]}/{action}',
                      headers={"Authorization": "Bearer " + record["token"]},
                      method="POST" if action == "stop" else "GET")
    with urlopen(request, timeout=3) as response:
        result = json.load(response)
    if result.get("pid") != record["pid"] or result.get("instance") != record["instance"]:
        raise ValueError("service_identity_mismatch")
    return result


def listening(port):
    with socket.socket() as connection:
        connection.settimeout(0.5)
        return connection.connect_ex(("127.0.0.1", port)) == 0


def status(service, port):
    record = read_state(state_path(service, port))
    if record and same_process(record):
        try:
            result = control(record, "status")
            result["port_listening"] = listening(port)
            from scripts.acceptance_report import source_snapshot
            current = source_snapshot()
            result["source_unchanged"] = (bool(current.get("source_sha256"))
                                          and current.get("source_sha256") == record.get("source", {}).get("source_sha256")
                                          and current.get("commit") == record.get("source", {}).get("commit"))
            return result
        except (OSError, ValueError, KeyError):
            return {"status": "unresponsive", "service": service, "port": port, "pid": record["pid"]}
    result = {"status": "unmanaged_port_in_use" if listening(port) else "stopped",
              "service": service, "port": port, "stale_record": record is not None}
    if record:
        result.update(last_status=record.get("status"), cleanup=record.get("cleanup", {}))
    return result


def start(service, port, *, no_schedules=False):
    current = status(service, port)
    if current["status"] == "running":
        return current
    if current["status"] != "stopped":
        raise ValueError("service_or_port_already_active")
    STATE.mkdir(parents=True, exist_ok=True)
    path = state_path(service, port)
    with launch_lock(path.with_suffix(".launch")):
        if status(service, port)["status"] != "stopped":
            raise ValueError("service_or_port_already_active")
        command = [sys.executable, str(Path(__file__).resolve()), "_run", service, "--port", str(port)]
        if no_schedules:
            command.append("--no-schedules")
        options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {"start_new_session": True}
        with path.with_suffix(".stdout.log").open("ab") as stdout, path.with_suffix(".stderr.log").open("ab") as stderr:
            process = subprocess.Popen(command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr, **options)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            current = status(service, port)
            if current["status"] == "running" and current.get("port_listening"):
                return current
            if process.poll() is not None:
                raise RuntimeError("service_start_failed_see_logs")
            time.sleep(0.2)
        raise TimeoutError("service_start_timeout_check_status_and_logs")


def stop(service, port):
    record = read_state(state_path(service, port))
    if not record or not same_process(record):
        current = status(service, port)
        if current["status"] == "stopped":
            return current
        raise ValueError("unmanaged_service_not_stopped")
    control(record, "stop")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if not same_process(record):
            return status(service, port)
        time.sleep(0.2)
    raise TimeoutError("graceful_stop_pending_resources_preserved")


def run(service, port, no_schedules):
    from dotenv import load_dotenv
    load_dotenv(ROOT / "knowledge/.env")
    if no_schedules:
        os.environ.update(DAILY_SUMMARY_ENABLED="0", MARKET_COLLECTION_ENABLED="0")
    import uvicorn
    from scripts.acceptance_report import source_snapshot
    snapshot = source_snapshot()
    if service == "query":
        from knowledge.api.query_api import create_app
    else:
        from knowledge.api.import_api import create_app
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    path = state_path(service, port)
    record = {"service": service, "port": port, "pid": os.getpid(), "create_time": psutil.Process().create_time(),
              "instance": secrets.token_hex(16), "token": secrets.token_hex(32), "status": "starting",
              "started_at": datetime.now(timezone.utc).isoformat(), "jobs": [],
              "source": {key: value for key, value in snapshot.items() if key != "files"}}

    def public():
        result = {key: value for key, value in record.items() if key not in {"token", "control_port"}}
        if result["status"] == "running" and not server.started:
            result["status"] = "starting"
        return result

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, action):
            self.connection.settimeout(3)
            if not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + record["token"]):
                self.send_error(403)
                return
            if self.path != "/" + action:
                self.send_error(404)
                return
            if action == "stop":
                record["status"] = "stopping"
                server.should_exit = True
            payload = public()
            if record["status"] == "running":
                payload["jobs"] = scheduled_jobs()
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply("status")

        def do_POST(self):
            self.reply("stop")

    def scheduled_jobs():
        result = []
        for name in ("summary_scheduler", "collection_scheduler"):
            scheduler = getattr(app.state, name, None)
            if scheduler is not None:
                result.extend({"id": job.id, "next_run_time": job.next_run_time.isoformat() if job.next_run_time else None,
                               "scheduler_running": scheduler.running} for job in scheduler.get_jobs())
        return result

    management = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    management.daemon_threads = True
    record["control_port"] = management.server_port
    write_state(path, record)
    thread = threading.Thread(target=management.serve_forever, daemon=True)
    thread.start()
    lifecycle = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(application):
        async with lifecycle(application):
            record.update(status="running", jobs=scheduled_jobs())
            write_state(path, record)
            try:
                yield
            finally:
                record["status"] = "stopping"
                write_state(path, record)

    app.router.lifespan_context = lifespan
    try:
        server.run()
    finally:
        management.shutdown()
        management.server_close()
        thread.join(timeout=3)
        runtime = getattr(app.state, "runtime", None)
        try:
            cleanup = runtime.close() if runtime is not None else {}
        except Exception:
            cleanup = {"runtime_closed": False}
        record.update(status="stopped" if all(cleanup.values()) else "cleanup_incomplete",
                      stopped_at=datetime.now(timezone.utc).isoformat(), cleanup=cleanup)
        record.pop("token", None)
        write_state(path, record)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "status", "stop", "_run"))
    parser.add_argument("service", choices=("query", "import"))
    parser.add_argument("--port", type=int)
    parser.add_argument("--no-schedules", action="store_true", help="显式关闭本次查询服务的自动任务")
    args = parser.parse_args()
    port = args.port if args.port is not None else (8001 if args.service == "query" else 8000)
    if not 1 <= port <= 65535:
        parser.error("port must be between 1 and 65535")
    try:
        if args.action == "_run":
            run(args.service, port, args.no_schedules)
            return 0
        result = (start(args.service, port, no_schedules=args.no_schedules) if args.action == "start"
                  else stop(args.service, port) if args.action == "stop" else status(args.service, port))
        print(json.dumps(result, ensure_ascii=False))
        return 0 if result["status"] in {"running", "stopped"} and all(result.get("cleanup", {}).values()) else 1
    except Exception as error:
        print(json.dumps({"error_type": type(error).__name__, "service": args.service, "port": port,
                          "message": "操作未完成；请查看状态与 .sandcastle/logs/services 中的日志"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
