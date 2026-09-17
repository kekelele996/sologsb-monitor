#!/usr/bin/env python3
"""Local HTTP service for the sologsb-0917 Pair-wise operations monitor."""
from __future__ import annotations

import argparse
import json
import mimetypes
import socket
import sys
import threading
import time
import urllib.parse
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from monitor_core import (
    APP_DIR,
    CONFIG_PATH,
    DEFAULT_ROOT,
    MonitorError,
    MonitorService,
    load_config,
    save_config,
    utc_now,
)

STATIC_DIR = APP_DIR / "static"
INDEX_PATH = STATIC_DIR / "index.html"


def lan_ip() -> str:
    for interface in ("en0", "en1", "en2"):
        try:
            import subprocess

            result = subprocess.run(
                ["ipconfig", "getifaddr", interface],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            value = result.stdout.strip()
            if value and not value.startswith("127."):
                return value
        except Exception:
            continue
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        value = sock.getsockname()[0]
        sock.close()
        return value
    except OSError:
        return ""


class MonitorHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, service: MonitorService):
        super().__init__(address, handler)
        self.service = service
        self.started_at = time.time()


class Handler(BaseHTTPRequestHandler):
    server_version = "sologsb-monitor/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def service(self) -> MonitorService:
        return self.server.service  # type: ignore[attr-defined]

    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), fmt % args))

    def _json(self, value: Any, status: int = 200) -> None:
        body = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._write(body)

    def _text(self, value: str, status: int = 200, content_type: str = "text/plain; charset=utf-8") -> None:
        body = value.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._write(body)

    def _bytes(self, body: bytes, content_type: str, status: int = 200) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self._write(body)

    def _write(self, body: bytes) -> None:
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        raw = self.rfile.read(max(0, min(length, 1024 * 1024)))
        if not raw:
            return {}
        value = json.loads(raw.decode("utf-8"))
        if not isinstance(value, dict):
            raise MonitorError("请求体必须是 JSON 对象")
        return value

    def _remote_actions_allowed(self) -> bool:
        cfg = self.service.config
        if (cfg.get("server") or {}).get("allowRemoteActions", False):
            return True
        remote = str(self.client_address[0] if self.client_address else "")
        return remote in {"127.0.0.1", "::1", "localhost"}

    def _serve_static(self, request_path: str) -> None:
        relative = request_path.lstrip("/") or "index.html"
        if relative == "":
            relative = "index.html"
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        try:
            body = candidate.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self._bytes(body, content_type)

    def do_HEAD(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        relative = "tasks.html" if path in {"/tasks", "/tasks/"} else ("index.html" if path in {"/", "/index.html"} else path.removeprefix("/static/"))
        if path not in {"/", "/index.html", "/tasks", "/tasks/"} and not path.startswith("/static/"):
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        candidate = (STATIC_DIR / relative).resolve()
        try:
            candidate.relative_to(STATIC_DIR.resolve())
        except ValueError:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        if candidate.suffix == ".html":
            content_type = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(candidate.stat().st_size))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path in {"/", "/index.html"}:
            self._serve_static("index.html")
            return
        if path in {"/tasks", "/tasks/"}:
            self._serve_static("tasks.html")
            return
        if path.startswith("/static/"):
            self._serve_static(path.removeprefix("/static/"))
            return
        if path == "/api/platform/projects":
            task_type = str((query.get("taskType") or ["0-1代码生成"])[0])
            force = str((query.get("refresh") or ["0"])[0]).lower() in {"1", "true", "yes"}
            try:
                self._json(self.service.platform_candidates(task_type=task_type, force=force))
            except MonitorError as exc:
                self._json({"error": str(exc), "items": []}, 400)
            except Exception as exc:
                self._json({"error": str(exc), "items": []}, 500)
            return
        if path == "/api/automation":
            try:
                self._json(self.service.queue.snapshot())
            except Exception as exc:
                self._json({"error": str(exc), "items": []}, 500)
            return
        if path == "/api/health":
            self._json(
                {
                    "ok": True,
                    "service": "sologsb-monitor",
                    "time": utc_now(),
                    "uptimeSeconds": round(time.time() - self.server.started_at, 1),  # type: ignore[attr-defined]
                }
            )
            return
        if path == "/api/snapshot":
            fetch_submissions = str((query.get("submissions") or ["1"])[0]).lower() not in {"0", "false", "no"}
            force_submissions = str((query.get("refresh") or ["0"])[0]).lower() in {"1", "true", "yes"}
            try:
                self._json(
                    self.service.snapshot(
                        fetch_submissions=fetch_submissions,
                        force_submissions=force_submissions,
                    )
                )
            except Exception as exc:
                self._json({"error": str(exc), "generatedAt": utc_now()}, 500)
            return
        if path == "/api/history":
            task_id = str((query.get("taskId") or [""])[0])
            side = str((query.get("side") or [""])[0])
            try:
                limit = int((query.get("limit") or ["400"])[0])
            except ValueError:
                limit = 400
            try:
                self._json(self.service.history(task_id, side, event_limit=limit))
            except MonitorError as exc:
                self._json({"error": str(exc), "attempts": []}, 400)
            except Exception as exc:
                self._json({"error": str(exc), "attempts": []}, 500)
            return
        if path == "/api/log":
            task_id = str((query.get("taskId") or [""])[0])
            side = str((query.get("side") or [""])[0])
            kind = str((query.get("kind") or ["events"])[0])
            try:
                lines = int((query.get("lines") or ["160"])[0])
            except ValueError:
                lines = 160
            try:
                self._json(self.service.tail_log(task_id, side, kind=kind, lines=lines))
            except MonitorError as exc:
                self._json({"error": str(exc), "lines": []}, 400)
            except Exception as exc:
                self._json({"error": str(exc), "lines": []}, 500)
            return
        if path == "/api/config":
            config = {key: value for key, value in self.service.config.items() if not key.startswith("_")}
            self._json(config)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path not in {"/api/action", "/api/auto", "/api/automation"}:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        if not self._remote_actions_allowed():
            self._json({"ok": False, "error": "远程客户端不允许执行操作"}, 403)
            return
        try:
            payload = self._read_json()
        except (ValueError, MonitorError) as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
            return
        try:
            if path == "/api/automation":
                result = self.service.automation_action(str(payload.get("action") or ""), payload)
                self._json({"ok": True, "automation": result})
                return
            if path == "/api/action":
                task_id = str(payload.get("taskId") or "")
                side = str(payload.get("side") or "")
                mode = str(payload.get("mode") or "resume")
                job = self.service.start_action(task_id, side, mode)
                self._json({"ok": True, "job": job})
                return
            enabled = bool(payload.get("enabled"))
            task_id = str(payload.get("taskId") or "").strip()
            task_root = None
            if task_id:
                task = self.service.task_by_id(task_id)
                if not task:
                    raise MonitorError("任务不存在")
                task_root = Path(task["taskRoot"])
            result = self.service.set_auto(enabled, task_root)
            self._json({"ok": True, **result})
        except MonitorError as exc:
            self._json({"ok": False, "error": str(exc)}, 400)
        except Exception as exc:
            self._json({"ok": False, "error": str(exc)}, 500)


def auto_loop(service: MonitorService, stop_event: threading.Event) -> None:
    auto_interval = float(((service.config.get("monitor") or {}).get("autoResume") or {}).get("tickSeconds") or 15)
    queue_interval = float((service.config.get("automation") or {}).get("tickSeconds") or 3)
    interval = max(1.0, min(auto_interval, queue_interval))
    while not stop_event.wait(interval):
        try:
            actions = service.maybe_auto_resume()
            for action in actions:
                print(f"[auto-resume] {action.get('message')}", flush=True)
        except Exception as exc:
            print(f"[auto-resume] tick failed: {exc}", file=sys.stderr, flush=True)
        try:
            queued = service.queue.tick()
            for action in queued:
                item = action.get("item") or {}
                job = action.get("job") or {}
                print(f"[queue] started {item.get('taskName')} / {item.get('side')} PID={job.get('pid')}", flush=True)
        except Exception as exc:
            print(f"[queue] tick failed: {exc}", file=sys.stderr, flush=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="sologsb-0917 A/B 任务监控")
    parser.add_argument("--root", action="append", type=Path, help="扫描根目录，可重复")
    parser.add_argument("--host", help="监听地址，默认读取 config.json")
    parser.add_argument("--port", type=int, help="监听端口，默认读取 config.json")
    parser.add_argument("--skill-script", type=Path, help="sologsb.py 路径")
    parser.add_argument("--no-submissions", action="store_true", help="禁用 SOLO2 提交信息")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    config = load_config(roots=[str(path) for path in args.root] if args.root else None)
    if args.host:
        config.setdefault("server", {})["host"] = args.host
    if args.port:
        config.setdefault("server", {})["port"] = int(args.port)
    if args.skill_script:
        config["skillScript"] = str(args.skill_script.expanduser().resolve())
    if args.no_submissions:
        config.setdefault("solo2", {})["enabled"] = False
    if not CONFIG_PATH.exists():
        save_config(config)

    service = MonitorService(config)
    host = str((config.get("server") or {}).get("host") or "127.0.0.1")
    port = int((config.get("server") or {}).get("port") or 8790)
    try:
        server = MonitorHTTPServer((host, port), Handler, service)
    except OSError as exc:
        print(f"无法监听 {host}:{port}: {exc}", file=sys.stderr)
        return 2

    stop_event = threading.Event()
    auto_thread = threading.Thread(target=auto_loop, args=(service, stop_event), daemon=True, name="auto-resume")
    auto_thread.start()
    print("sologsb-monitor 已启动")
    print(f"本机访问: http://127.0.0.1:{port}")
    if host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"局域网访问: http://{ip}:{port}")
        print("提示：默认远程客户端只能查看，不能执行续跑操作。")
    print(f"扫描目录: {', '.join(str(item) for item in config.get('roots') or [DEFAULT_ROOT])}")
    print(f"技能 CLI: {config.get('skillScript')}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        stop_event.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
