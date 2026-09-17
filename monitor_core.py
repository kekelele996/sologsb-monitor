#!/usr/bin/env python3
"""Core collectors for the local sologsb-0917 operations monitor.

The module intentionally uses only the Python standard library.  It treats
``monitor/state.json``, Claude Code JSONL and Docker as read-only sources; the
only mutating actions are explicit calls to the sologsb-0917 CLI.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from codex_sessions import active_sessions as scan_codex_sessions, queue_prompt_sha256

APP_DIR = Path(__file__).resolve().parent
DEFAULT_ROOT = APP_DIR.parent
DEFAULT_SKILL_SCRIPT = Path.home() / ".codex" / "skills" / "sologsb-0917" / "scripts" / "sologsb.py"
PLATFORM_SCRIPTS = Path.home() / ".codex" / "skills" / "solo-annotation-loop" / "scripts"
CONFIG_PATH = APP_DIR / "config.json"
STATE_DIR = APP_DIR / ".state"
AUTO_STATE_PATH = STATE_DIR / "auto.json"
QUEUE_STATE_PATH = STATE_DIR / "queue.json"
DISMISSED_TASKS_PATH = STATE_DIR / "dismissed-tasks.json"
AUTO_LOG_LIMIT = 120
EVENT_LIMIT = 80
DEFAULT_PAGE_SIZE = 100
SUBS_TTL = 60

SIDES = ("A", "B")
TERMINAL_TASK_STATUSES = {
    "semantic_review_required",
    "ab_clean",
    "verified",
    "gsb_ready",
    "recorded",
    "complete",
}
RUNNABLE_TASK_STATUSES = {
    "repo_ready",
    "running",
    "a_staged",
    "b_staged",
    "semantic_review_required",
    "attempt_invalid",
    "blocked",
}
SIDE_DONE_STATUSES = {"staged", "clean"}
SIDE_FAILED_STATUSES = {"attempt_invalid", "blocked", "invalidated"}
CANDIDATE_ID_RE = re.compile(r"candidate-[1-9][0-9]*")

DEFAULT_AUTO_TRIGGER_PROMPT = '使用 `$sologsb-0917`，在当前目录为每道任务创建独立目录，完整执行一条 Pair-wise GSB。仅本地交付：严禁提交 GSB 表单，严禁调用未授权的写接口。\n\n- 选定项目：`{{selected_project}}`\n- 凭据只从环境变量或系统 Keychain 读取，禁止写入任务文件、日志或提示词。\n- 如果选定项目为空，则从已配置的项目源选择项目：任务类型由监控台配置，难度使用 `困难`。必须记录并汇报任务标识、变体、配额消耗和选择结果。不得静默换题；无法按要求使用所选项目时立即停止。\n- 严格执行技能最新流程：先预拉 3 份候选并并行运行；前两名按完成顺序映射 A/B；映射后再创建 GitHub `main/A/B` 并发布产物。\n- 所有候选共用同一份 UTF-8 提示词，字节完全一致；不得选择“代码理解”。\n- 真实执行构建、测试或启动验证，结论必须绑定轨迹、commit 或命令输出。\n- GSB 理由 150–240 个非空白字符；负面判断必须写具体触发节点，禁止泛化表述。\n- A/B 分别使用 Otty 录制真实视频，1280x720；失败也必须保留真实过程，禁止 headless 或伪造，录屏只允许出现终端和浏览器页面。\n- 任何门禁不通过立即停止并报告准确原因。\n\n最终只汇报：仓库地址、初始 SHA、A/B SessionID 与 commit、轨迹、审核结论、GSB 文案、20 字段 Excel、字段说明、两段视频、未解决问题。'

DEFAULT_CONFIG: dict[str, Any] = {
    "roots": [str(DEFAULT_ROOT)],
    "skillScript": str(DEFAULT_SKILL_SCRIPT),
    "server": {
        "host": "127.0.0.1",
        "port": 8790,
        "allowRemoteActions": False,
    },
    "platform": {
        "managerBaseUrl": os.environ.get("SOLO_MANAGER_BASE_URL", "").rstrip("/"),
        "candidateTtlSeconds": 30,
        "taskType": "0-1代码生成",
        "difficulty": "困难",
    },
    "automation": {
        "tickSeconds": 3,
        "capacity": 2,
        "cooldownSeconds": 200,
        "paused": True,
        "promptTemplate": DEFAULT_AUTO_TRIGGER_PROMPT,
    },
    "monitor": {
        "pollSeconds": 3,
        "dockerCacheSeconds": 2,
        "traceMaxBytes": 16 * 1024 * 1024,
        "autoResume": {
            "enabled": False,
            "tickSeconds": 15,
            "staleSeconds": 420,
            "cooldownSeconds": 180,
            "maxRelaunchesPerSide": 3,
            "windowSeconds": 3600,
            "maxConcurrentJobs": 2,
        },
    },
    "solo2": {
        "enabled": True,
        "apiBaseUrl": (os.environ.get("SOLO2_SERVER", "").rstrip("/") + "/api/v1") if os.environ.get("SOLO2_SERVER", "").strip() else "",
        "monitorUrl": "http://127.0.0.1:8787",
        "preferMonitor": True,
        "pageSize": DEFAULT_PAGE_SIZE,
    },
}


def render_auto_trigger_prompt(
    template: str,
    project: dict[str, Any] | None,
    *,
    task_type: str = "0-1代码生成",
    difficulty: str = "困难",
) -> str:
    """Render the platform task prompt with the selected project snapshot."""
    project = project if isinstance(project, dict) else {}
    code = str(project.get("code") or "").strip()
    name = str(project.get("name") or "").strip()
    selected = " · ".join(part for part in (code, name) if part) or "未指定（由执行器从 Solo Manager 选择）"
    replacements = {
        "{{selected_project}}": selected,
        "{{project_code}}": code,
        "{{project_name}}": name,
        "{{task_type}}": task_type,
        "{{difficulty}}": difficulty,
    }
    rendered = str(template or DEFAULT_AUTO_TRIGGER_PROMPT)
    for marker, value in replacements.items():
        rendered = rendered.replace(marker, value)
    return rendered


class MonitorError(RuntimeError):
    """Raised for expected monitor failures."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_time(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_from_timestamp(value: float | None) -> str:
    if not value:
        return ""
    return datetime.fromtimestamp(value, timezone.utc).isoformat().replace("+00:00", "Z")


def age_seconds(value: Any, now: float | None = None) -> float | None:
    dt = parse_time(value)
    if dt is None:
        return None
    current = datetime.now(timezone.utc) if now is None else datetime.fromtimestamp(now, timezone.utc)
    return max(0.0, (current - dt).total_seconds())


def read_json(path: Path, default: Any = None) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temp, path)


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def load_config(path: Path | None = None, roots: Iterable[str] | None = None) -> dict[str, Any]:
    config_path = Path(path or CONFIG_PATH)
    raw = read_json(config_path, {})
    if not isinstance(raw, dict):
        raw = {}
    config = deep_merge(DEFAULT_CONFIG, raw)
    if roots:
        config["roots"] = [str(Path(item).expanduser().resolve()) for item in roots]
    config["_configPath"] = str(config_path)
    return config


def save_config(config: dict[str, Any], path: Path | None = None) -> None:
    target = Path(path or CONFIG_PATH)
    clean = {key: value for key, value in config.items() if not key.startswith("_")}
    atomic_write_json(target, clean)


def safe_slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-._")
    return cleaned or "task"


def short_hash(value: str, length: int = 12) -> str:
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def truncate(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)] + "…"


_SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|apikey|token|password|secret|cookie|csrf)\s*[=:]\s*([^\s,;]+)"),
    re.compile(r"(?i)authorization\s*:\s*bearer\s+[^\s]+"),
    re.compile(r"sk-[A-Za-z0-9_-]{8,}"),
)


def redact_text(value: Any, limit: int = 500) -> str:
    text = truncate(value, limit)
    for pattern in _SECRET_PATTERNS:
        if pattern.pattern.startswith("(?i)authorization"):
            text = pattern.sub("authorization: Bearer ***", text)
        elif pattern.pattern.startswith("sk-"):
            text = pattern.sub("sk-***", text)
        else:
            text = pattern.sub(lambda m: f"{m.group(1)}=***", text)
    return text


def pid_alive(pid: Any) -> bool:
    try:
        value = int(pid)
    except (TypeError, ValueError):
        return False
    if value <= 0:
        return False
    try:
        os.kill(value, 0)
        return True
    except OSError:
        return False


def pid_command(pid: Any) -> str:
    try:
        result = subprocess.run(
            ["ps", "-p", str(int(pid)), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
            check=False,
        )
        return result.stdout.strip()
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""


def persisted_job_process_alive(job: dict[str, Any]) -> bool:
    pid = job.get("pid")
    if not pid_alive(pid):
        return False
    command = pid_command(pid)
    if not command:
        return True
    lowered = command.lower()
    return "queue_worker.py" in lowered or "sologsb" in lowered


def runner_pid_alive(record: dict[str, Any], task_root: Path, side: str) -> bool:
    pid = record.get("runPid")
    if not pid_alive(pid):
        return False
    command = pid_command(pid)
    if not command:
        return True
    side_text = str(side).upper()
    side_ok = f"--side {side_text}" in command or "--side both" in command
    task_ok = str(task_root) in command or "sologsb" in command.lower()
    return bool(side_ok and task_ok)


def _is_task_state(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    if not str(state.get("taskName") or "").strip():
        return False
    if isinstance(state.get("sides"), dict):
        return True
    return bool(state.get("initialSnapshot") or state.get("promptPath"))


def discover_task_roots(roots: Iterable[str | Path], max_depth: int = 5) -> list[Path]:
    found: list[Path] = []
    seen: set[str] = set()
    prune = {".git", "__pycache__", "node_modules", ".venv", "venv", ".idea", ".vscode", "source", "workspace"}
    for root_value in roots:
        root = Path(root_value).expanduser().resolve()
        if not root.is_dir():
            continue
        for current, dirnames, _filenames in os.walk(root):
            path = Path(current)
            try:
                relative_depth = len(path.relative_to(root).parts)
            except ValueError:
                relative_depth = 99
            state_path = path / "monitor" / "state.json"
            state = read_json(state_path, {})
            if _is_task_state(state):
                key = str(path.resolve())
                if key not in seen:
                    seen.add(key)
                    found.append(path.resolve())
                dirnames[:] = []
                continue
            if relative_depth >= max_depth:
                dirnames[:] = []
                continue
            dirnames[:] = [name for name in dirnames if name not in prune and not name.startswith(".")]
    return sorted(found, key=lambda item: item.name.lower())


def _git_head(repo: Path) -> str:
    git_dir = repo / ".git"
    if not git_dir.is_dir():
        return ""
    head = git_dir / "HEAD"
    try:
        value = head.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    if value.startswith("ref: "):
        ref = value[5:].strip()
        ref_file = git_dir / ref
        try:
            return ref_file.read_text(encoding="utf-8").strip()[:12]
        except OSError:
            try:
                for line in (git_dir / "packed-refs").read_text(encoding="utf-8").splitlines():
                    if line.startswith("#") or not line.strip():
                        continue
                    parts = line.split()
                    if len(parts) >= 2 and parts[1] == ref:
                        return parts[0][:12]
            except OSError:
                pass
            return ""
    return value[:12]


def _tool_detail(block: dict[str, Any]) -> str:
    name = str(block.get("name") or "Tool")
    payload = block.get("input") if isinstance(block.get("input"), dict) else {}
    if name == "Bash":
        return redact_text(payload.get("command") or "", 420)
    if name in {"Read", "Write", "Edit"}:
        return redact_text(payload.get("file_path") or payload.get("path") or "", 240)
    if name in {"Glob", "Grep"}:
        return redact_text(
            " ".join(str(payload.get(key) or "") for key in ("pattern", "path") if payload.get(key)),
            240,
        )
    try:
        return redact_text(json.dumps(payload, ensure_ascii=False), 360)
    except (TypeError, ValueError):
        return ""


def _result_text(content: Any) -> str:
    if isinstance(content, str):
        return redact_text(content, 420)
    if isinstance(content, list):
        pieces: list[str] = []
        for item in content:
            if isinstance(item, dict):
                pieces.append(str(item.get("text") or item.get("content") or ""))
            else:
                pieces.append(str(item))
        return redact_text(" ".join(piece for piece in pieces if piece), 420)
    return redact_text(content, 420)


def _content_blocks(event: dict[str, Any]) -> list[dict[str, Any]]:
    message = event.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, list):
        return []
    return [item for item in content if isinstance(item, dict)]


def _new_trace_stats() -> dict[str, Any]:
    return {
        "sessionId": "",
        "model": "",
        "harnessVersion": "",
        "eventCount": 0,
        "assistantTurns": 0,
        "toolCalls": 0,
        "toolResults": 0,
        "thinkingTokens": 0,
        "apiRetries": 0,
        "compactions": 0,
        "commands": 0,
        "filesTouched": [],
        "lastPhase": "starting",
        "lastEventType": "",
        "lastSummary": "等待首个执行事件",
        "lastText": "",
        "lastTool": "",
        "lastResult": "",
        "lastError": "",
        "result": {},
        "todos": [],
        "todoUpdatedAt": "",
        "recent": [],
        "recentLimit": EVENT_LIMIT,
        "seq": 0,
    }


def _push_event(stats: dict[str, Any], kind: str, title: str, detail: str = "") -> None:
    stats["seq"] += 1
    item = {
        "seq": stats["seq"],
        "kind": kind,
        "title": title,
        "detail": redact_text(detail, 500),
        "at": str(stats.get("currentEventAt") or ""),
    }
    stats["recent"].append(item)
    recent_limit = int(stats.get("recentLimit") or EVENT_LIMIT)
    if len(stats["recent"]) > recent_limit:
        del stats["recent"][: len(stats["recent"]) - recent_limit]


def _touch_file(stats: dict[str, Any], path: str) -> None:
    value = str(path or "").strip()
    if not value:
        return
    touched = stats["filesTouched"]
    if value not in touched:
        touched.append(value)
    if len(touched) > 80:
        del touched[: len(touched) - 80]


def process_trace_event(stats: dict[str, Any], event: dict[str, Any]) -> None:
    stats["eventCount"] += 1
    event_type = str(event.get("type") or "")
    stats["lastEventType"] = event_type
    stats["currentEventAt"] = str(event.get("timestamp") or "")
    if not stats.get("sessionId"):
        stats["sessionId"] = str(event.get("sessionId") or event.get("session_id") or "")

    if event_type == "system":
        subtype = str(event.get("subtype") or "")
        if subtype == "init":
            stats["sessionId"] = str(event.get("session_id") or event.get("sessionId") or stats["sessionId"])
            stats["model"] = str(event.get("model") or stats["model"])
            stats["harnessVersion"] = str(event.get("claude_code_version") or stats["harnessVersion"])
            stats["lastPhase"] = "thinking"
            stats["lastSummary"] = "会话已建立，等待模型开始执行"
            _push_event(stats, "system", "会话已建立", stats["model"])
        elif subtype == "thinking_tokens":
            delta = event.get("estimated_tokens_delta")
            total = event.get("estimated_tokens")
            if isinstance(total, (int, float)):
                stats["thinkingTokens"] = int(total)
            elif isinstance(delta, (int, float)):
                stats["thinkingTokens"] += int(delta)
            stats["lastPhase"] = "thinking"
            stats["lastSummary"] = "模型正在思考"
        elif subtype in {"api_retry", "api_error"}:
            stats["apiRetries"] += 1
            attempt = event.get("attempt")
            maximum = event.get("max_retries")
            status = event.get("error_status") or event.get("error") or subtype
            message = f"接口重试 {attempt or '-'} / {maximum or '-'}，{status}"
            stats["lastPhase"] = "retrying"
            stats["lastSummary"] = message
            stats["lastError"] = message
            _push_event(stats, "warning", "接口重试", message)
        elif subtype in {"compact_boundary", "compact"}:
            stats["compactions"] += 1
            stats["lastSummary"] = "上下文压缩边界"
            _push_event(stats, "system", "上下文压缩", f"第 {stats['compactions']} 次")
        return

    if event_type == "assistant":
        stats["assistantTurns"] += 1
        for block in _content_blocks(event):
            block_type = str(block.get("type") or "")
            if block_type == "thinking":
                stats["lastPhase"] = "thinking"
                stats["lastSummary"] = "模型正在思考"
                _push_event(stats, "thinking", "思考")
            elif block_type == "text":
                text = redact_text(block.get("text") or "", 520)
                if text:
                    stats["lastPhase"] = "responding"
                    stats["lastText"] = text
                    stats["lastSummary"] = text
                    _push_event(stats, "assistant", "模型输出", text)
            elif block_type == "tool_use":
                name = str(block.get("name") or "Tool")
                detail = _tool_detail(block)
                stats["toolCalls"] += 1
                stats["lastTool"] = name
                stats["lastPhase"] = f"tool:{name}"
                stats["lastSummary"] = f"调用 {name}" + (f"：{detail}" if detail else "")
                if name == "Bash":
                    stats["commands"] += 1
                if name in {"Write", "Edit"}:
                    payload = block.get("input") if isinstance(block.get("input"), dict) else {}
                    _touch_file(stats, str(payload.get("file_path") or payload.get("path") or ""))
                if name == "TodoWrite":
                    payload = block.get("input") if isinstance(block.get("input"), dict) else {}
                    todos = []
                    for item in payload.get("todos") or []:
                        if not isinstance(item, dict):
                            continue
                        content = redact_text(item.get("content") or item.get("activeForm") or "", 500)
                        if not content:
                            continue
                        status = str(item.get("status") or "pending")
                        if status not in {"pending", "in_progress", "completed"}:
                            status = "pending"
                        todos.append({
                            "content": content,
                            "activeForm": redact_text(item.get("activeForm") or "", 500),
                            "status": status,
                        })
                    stats["todos"] = todos
                    stats["todoUpdatedAt"] = str(stats.get("currentEventAt") or "")
                    completed = sum(1 for item in todos if item.get("status") == "completed")
                    detail = f"{completed}/{len(todos)} 完成" if todos else "清空待办"
                _push_event(stats, "todo" if name == "TodoWrite" else "tool", f"调用 {name}", detail)
        return

    if event_type == "user":
        for block in _content_blocks(event):
            block_type = str(block.get("type") or "")
            if block_type == "text":
                text = redact_text(block.get("text") or "", 420)
                if text:
                    stats["lastPhase"] = "prompt"
                    stats["lastSummary"] = text
                    _push_event(stats, "user", "真人输入", text)
                continue
            if block_type != "tool_result":
                continue
            stats["toolResults"] += 1
            text = _result_text(block.get("content"))
            stats["lastResult"] = text
            stats["lastPhase"] = "tool_result"
            stats["lastSummary"] = text or "工具已返回"
            if block.get("is_error"):
                stats["lastError"] = text or "工具执行失败"
                _push_event(stats, "error", "工具失败", stats["lastError"])
            else:
                _push_event(stats, "result", "工具返回", text)
        return

    if event_type == "queue-operation":
        operation = str(event.get("operation") or "queue")
        detail = redact_text(event.get("content") or event.get("message") or operation, 420)
        stats["lastPhase"] = "queued"
        stats["lastSummary"] = f"队列操作 {operation}"
        _push_event(stats, "system", f"队列 {operation}", detail)
        return

    if event_type == "result":
        stats["result"] = {
            key: event.get(key)
            for key in ("subtype", "stop_reason", "num_turns", "duration_ms", "total_cost_usd", "is_error")
            if event.get(key) is not None
        }
        stop_reason = str(event.get("stop_reason") or "")
        if event.get("is_error"):
            stats["lastPhase"] = "error"
            stats["lastError"] = redact_text(event.get("error") or event.get("result") or "回合异常结束", 420)
            _push_event(stats, "error", "回合异常结束", stats["lastError"])
        else:
            stats["lastPhase"] = "done"
            stats["lastSummary"] = f"回合结束 stop_reason={stop_reason or '-'}"
            _push_event(stats, "done", "回合结束", stats["lastSummary"])


class TraceCache:
    """Incrementally parse stream-json logs and retain bounded recent events."""

    def __init__(self, max_bytes: int = 16 * 1024 * 1024):
        self.max_bytes = max_bytes
        self._lock = threading.RLock()
        self._entries: dict[str, dict[str, Any]] = {}

    def stats(self, path: Path | None) -> dict[str, Any]:
        if path is None:
            return _new_trace_stats()
        key = str(path)
        try:
            stat = path.stat()
        except OSError:
            return _new_trace_stats()
        if stat.st_size > self.max_bytes:
            return self._tail_stats(path, stat.st_mtime)

        with self._lock:
            entry = self._entries.get(key)
            identity = (stat.st_ino, stat.st_dev)
            if not entry or entry.get("identity") != identity or stat.st_size < entry.get("offset", 0):
                entry = {
                    "identity": identity,
                    "offset": 0,
                    "partial": b"",
                    "stats": _new_trace_stats(),
                }
                self._entries[key] = entry
            offset = int(entry.get("offset") or 0)
            if stat.st_size > offset:
                try:
                    with path.open("rb") as stream:
                        stream.seek(offset)
                        chunk = stream.read(stat.st_size - offset)
                except OSError:
                    chunk = b""
                entry["offset"] = offset + len(chunk)
                combined = bytes(entry.get("partial") or b"") + chunk
                lines = combined.split(b"\n")
                entry["partial"] = lines.pop() if lines else b""
                for raw in lines:
                    if not raw.strip():
                        continue
                    try:
                        event = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        process_trace_event(entry["stats"], event)
            entry["mtime"] = stat.st_mtime
            entry["size"] = stat.st_size
            return copy.deepcopy(entry["stats"])

    def _tail_stats(self, path: Path, mtime: float) -> dict[str, Any]:
        stats = _new_trace_stats()
        try:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - self.max_bytes))
                lines = stream.read(self.max_bytes).splitlines()
        except OSError:
            return stats
        for raw in lines:
            try:
                event = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(event, dict):
                process_trace_event(stats, event)
        return stats


def read_trace_stats(path: Path | None, *, event_limit: int = 400, max_bytes: int = 32 * 1024 * 1024) -> dict[str, Any]:
    """Parse one trace file on demand and retain a larger event window for history views."""
    stats = _new_trace_stats()
    stats["recentLimit"] = max(1, min(int(event_limit), 2000))
    if path is None or not path.is_file():
        return stats
    try:
        size = path.stat().st_size
        with path.open("rb") as stream:
            if size > max_bytes:
                stream.seek(size - max_bytes)
            for raw in stream.read(max_bytes).splitlines():
                try:
                    event = json.loads(raw.decode("utf-8", errors="replace"))
                except ValueError:
                    continue
                if isinstance(event, dict):
                    process_trace_event(stats, event)
    except OSError:
        return stats
    return stats


class DockerCache:
    def __init__(self, ttl: float = 2.0):
        self.ttl = ttl
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: dict[str, Any] = {"items": [], "byName": {}, "error": ""}

    @staticmethod
    def _docker_json(value: Any) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except (TypeError, ValueError):
            return {}

    def get(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if not force and self._data and now - self._at < self.ttl:
                return copy.deepcopy(self._data)
        try:
            result = subprocess.run(
                ["docker", "ps", "-a", "--no-trunc", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
                timeout=8,
                check=False,
            )
            if result.returncode != 0:
                data = {"items": [], "byName": {}, "error": (result.stderr or "docker ps 失败").strip()}
            else:
                items: list[dict[str, Any]] = []
                by_name: dict[str, dict[str, Any]] = {}
                for line in result.stdout.splitlines():
                    parsed = self._docker_json(line)
                    if not parsed:
                        continue
                    item = {
                        "id": str(parsed.get("ID") or ""),
                        "name": str(parsed.get("Names") or ""),
                        "state": str(parsed.get("State") or "").lower(),
                        "status": str(parsed.get("Status") or ""),
                        "createdAt": str(parsed.get("CreatedAt") or ""),
                        "image": str(parsed.get("Image") or ""),
                        "labels": str(parsed.get("Labels") or ""),
                    }
                    items.append(item)
                    if item["name"]:
                        by_name[item["name"]] = item
                data = {"items": items, "byName": by_name, "error": ""}
        except (OSError, subprocess.TimeoutExpired) as exc:
            data = {"items": [], "byName": {}, "error": f"Docker 不可用：{exc}"}
        with self._lock:
            self._at = now
            self._data = data
            return copy.deepcopy(data)


def _container_for_side(
    task_name: str,
    side: str,
    state_side: dict[str, Any],
    attempt: dict[str, Any] | None,
    docker: dict[str, Any],
) -> dict[str, Any]:
    by_name = docker.get("byName") or {}
    names: list[str] = []
    if isinstance(attempt, dict):
        container = attempt.get("container")
        if isinstance(container, dict) and container.get("name"):
            names.append(str(container["name"]))
        if attempt.get("container") and isinstance(attempt.get("container"), str):
            names.append(str(attempt["container"]))
    container = state_side.get("container")
    if isinstance(container, dict) and container.get("name"):
        names.append(str(container["name"]))
    elif container:
        names.append(str(container))
    for name in names:
        if name in by_name:
            return copy.deepcopy(by_name[name])
    prefix = f"sologsb-{safe_slug(task_name)}-{side.lower()}-"
    candidates = [
        item for item in (docker.get("items") or [])
        if str(item.get("name") or "").startswith(prefix)
    ]
    if candidates:
        candidates.sort(key=lambda item: str(item.get("createdAt") or ""), reverse=True)
        return copy.deepcopy(candidates[0])
    return {}


def _is_candidate_id(value: Any) -> bool:
    return bool(CANDIDATE_ID_RE.fullmatch(str(value or "")))


def _runtime_root(task_root: Path, identifier: str) -> Path:
    if _is_candidate_id(identifier):
        return task_root / "monitor" / "runtime" / "candidates" / identifier
    return task_root / "monitor" / "runtime" / identifier.lower()


def _rejected_root(task_root: Path, identifier: str) -> Path:
    if _is_candidate_id(identifier):
        return task_root / "workspace" / "轨迹文件" / "candidates" / identifier / "rejected"
    return task_root / "workspace" / "轨迹文件" / identifier.lower() / "rejected"


def _attempt_dirs(task_root: Path, side: str) -> list[Path]:
    root = _runtime_root(task_root, side)
    if not root.is_dir():
        return []
    items = [item for item in root.glob("attempt-*") if item.is_dir()]
    def attempt_no(item: Path) -> int:
        match = re.search(r"(\d+)$", item.name)
        return int(match.group(1)) if match else 0
    return sorted(items, key=attempt_no)


def _attempt_no(path: Path) -> int:
    match = re.search(r"(\d+)$", path.name)
    return int(match.group(1)) if match else 0


def _trace_session_id(path: Path | None, trace_cache: TraceCache) -> str:
    if path is None:
        return ""
    stats = trace_cache.stats(path)
    sid = str(stats.get("sessionId") or "").strip()
    if sid:
        return sid
    if path.name.endswith(".jsonl") and path.stem != "stdout":
        candidate = path.stem.strip()
        if re.fullmatch(r"[0-9a-fA-F-]{32,40}", candidate):
            return candidate
    return ""


def _container_name(value: Any) -> str:
    if isinstance(value, dict):
        return str(value.get("name") or "")
    return str(value or "")


def _attempt_history(
    task_root: Path,
    side: str,
    state_side: dict[str, Any],
    trace_cache: TraceCache,
    docker: dict[str, Any],
    *,
    include_events: bool = False,
    event_limit: int = 400,
) -> list[dict[str, Any]]:
    """Build a de-duplicated history of current runtime and rejected attempts.

    Attempt numbers restart after a side is rerun, therefore SessionID is the
    primary identity.  Runtime and rejected copies of the same session are
    merged; sessions without an ID remain separate by source path.
    """
    records: list[dict[str, Any]] = []
    by_session: dict[str, dict[str, Any]] = {}
    current_attempt = int(state_side.get("attempt") or 0)
    current_status = str(state_side.get("status") or "")
    result_record = read_json(_runtime_root(task_root, side) / "result.json", {})
    if not isinstance(result_record, dict):
        result_record = {}

    def add_record(record: dict[str, Any]) -> dict[str, Any]:
        sid = str(record.get("sessionId") or "")
        if sid and sid in by_session:
            existing = by_session[sid]
            existing["finishedAt"] = existing.get("finishedAt") or record.get("finishedAt") or ""
            existing["error"] = existing.get("error") or record.get("error") or ""
            existing["rejectionReason"] = existing.get("rejectionReason") or record.get("rejectionReason") or ""
            if existing.get("status") == "running" and record.get("source") == "rejected":
                existing["status"] = "attempt_invalid"
            sources = existing.setdefault("sources", [existing.get("source", "")])
            if record.get("source") and record.get("source") not in sources:
                sources.append(record.get("source"))
            return existing
        record.setdefault("sources", [record.get("source", "")])
        records.append(record)
        if sid:
            by_session[sid] = record
        return record

    for attempt_dir in _attempt_dirs(task_root, side):
        number = _attempt_no(attempt_dir)
        meta = read_json(attempt_dir / "attempt.json", {})
        if not isinstance(meta, dict):
            meta = {}
        stdout_path = attempt_dir / "stdout.jsonl"
        trace_path = stdout_path if stdout_path.is_file() else None
        stats = read_trace_stats(trace_path, event_limit=event_limit) if include_events else trace_cache.stats(trace_path)
        sid = str(meta.get("sessionId") or stats.get("sessionId") or "")
        is_current = bool(number == current_attempt or (sid and sid == str(state_side.get("sessionId") or "")))
        status = current_status if is_current and current_status else "archived"
        error = str(state_side.get("error") or "") if is_current else ""
        started_at = str(meta.get("startedAt") or state_side.get("startedAt") or "") if is_current else str(meta.get("startedAt") or "")
        finished_at = ""
        if is_current and current_status in SIDE_DONE_STATUSES:
            finished_at = str(state_side.get("stagedAt") or state_side.get("publishedAt") or result_record.get("stagedAt") or "")
        record = {
            "key": f"session:{sid}" if sid else f"runtime:{number}:{attempt_dir.stat().st_mtime_ns}",
            "attempt": number,
            "source": "runtime",
            "sources": ["runtime"],
            "isCurrent": is_current,
            "status": status,
            "sessionId": sid,
            "startedAt": started_at,
            "finishedAt": finished_at,
            "durationSeconds": age_seconds(started_at),
            "model": str(meta.get("model") or stats.get("model") or ""),
            "harnessVersion": str(meta.get("harnessVersion") or stats.get("harnessVersion") or ""),
            "contextWindow": meta.get("declaredContextWindow"),
            "container": _container_name(meta.get("container")),
            "error": error,
            "rejectionReason": "",
            "tracePath": str(trace_path or ""),
            "stdoutPath": str(stdout_path) if stdout_path.is_file() else "",
            "stderrPath": str(attempt_dir / "stderr.log") if (attempt_dir / "stderr.log").is_file() else "",
            "rejectedPath": "",
            "validation": meta.get("validation") if isinstance(meta.get("validation"), dict) else {},
            "trace": {
                key: stats.get(key)
                for key in (
                    "eventCount", "assistantTurns", "toolCalls", "toolResults", "thinkingTokens",
                    "apiRetries", "compactions", "commands", "filesTouched", "lastPhase",
                    "lastSummary", "lastText", "lastTool", "lastResult", "lastError", "result",
                    "todos", "todoUpdatedAt",
                )
            },
        }
        if include_events:
            record["events"] = list(stats.get("recent") or [])
        add_record(record)

    rejected_root = _rejected_root(task_root, side)
    if rejected_root.is_dir():
        for rejected_dir in sorted(
            (item for item in rejected_root.glob("attempt-*") if item.is_dir()),
            key=_attempt_no,
        ):
            number = _attempt_no(rejected_dir)
            rejection = read_json(rejected_dir / "rejection.json", {})
            if not isinstance(rejection, dict):
                rejection = {}
            candidates = sorted(
                (item for item in rejected_dir.glob("*.jsonl") if item.is_file() and item.name != "stdout.jsonl"),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
            stdout_path = rejected_dir / "stdout.jsonl"
            trace_path = candidates[0] if candidates else (stdout_path if stdout_path.is_file() else None)
            stats = read_trace_stats(trace_path, event_limit=event_limit) if include_events else trace_cache.stats(trace_path)
            sid = _trace_session_id(trace_path, trace_cache) or str(stats.get("sessionId") or "")
            rejected_at = str(rejection.get("recordedAt") or "")
            reason = str(rejection.get("reason") or "历史尝试未通过单轮校验")
            record = {
                "key": f"rejected:{number}:{sid or rejected_dir.stat().st_mtime_ns}",
                "attempt": number,
                "source": "rejected",
                "sources": ["rejected"],
                "isCurrent": False,
                "status": "attempt_invalid",
                "sessionId": sid,
                "startedAt": "",
                "finishedAt": rejected_at,
                "durationSeconds": None,
                "model": str(stats.get("model") or ""),
                "harnessVersion": str(stats.get("harnessVersion") or ""),
                "contextWindow": None,
                "container": "",
                "error": reason,
                "rejectionReason": reason,
                "tracePath": str(trace_path or ""),
                "stdoutPath": str(stdout_path) if stdout_path.is_file() else "",
                "stderrPath": str(rejected_dir / "stderr.log") if (rejected_dir / "stderr.log").is_file() else "",
                "rejectedPath": str(rejected_dir),
                "validation": {},
                "trace": {
                    key: stats.get(key)
                    for key in (
                        "eventCount", "assistantTurns", "toolCalls", "toolResults", "thinkingTokens",
                        "apiRetries", "compactions", "commands", "filesTouched", "lastPhase",
                        "lastSummary", "lastText", "lastTool", "lastResult", "lastError", "result",
                    )
                },
            }
            if include_events:
                record["events"] = list(stats.get("recent") or [])
            add_record(record)

    def sort_key(item: dict[str, Any]) -> tuple[float, int, str]:
        timestamp = parse_time(item.get("startedAt")) or parse_time(item.get("finishedAt"))
        return (
            timestamp.timestamp() if timestamp else 0.0,
            int(item.get("attempt") or 0),
            str(item.get("key") or ""),
        )

    records.sort(key=sort_key)
    return records


def _phase_for_side(state_side: dict[str, Any], trace: dict[str, Any]) -> tuple[str, str]:
    raw = str(state_side.get("status") or "")
    if raw == "staged":
        return "staged", "结构校验通过，等待 A/B 双侧审核发布"
    if raw == "clean":
        return "clean", "已发布"
    if raw == "blocked":
        return "blocked", "连续失败，已阻断"
    if raw == "attempt_invalid":
        return "failed", "本轮无效，需要重跑"
    if raw == "invalidated":
        return "failed", "现场已作废，需要重跑"
    if raw == "cancelled":
        return "cancelled", "已有两个候选先完成，此候选已主动停止"
    if raw == "running":
        phase = str(trace.get("lastPhase") or "starting")
        mapping = {
            "starting": "starting",
            "thinking": "thinking",
            "responding": "responding",
            "retrying": "retrying",
            "tool_result": "tool_result",
            "done": "finishing",
            "error": "error",
        }
        if phase.startswith("tool:"):
            return "tool", phase.split(":", 1)[1]
        return mapping.get(phase, "running"), phase
    return "idle", raw or "等待启动"


def _progress_stages(trace: dict[str, Any], state_side: dict[str, Any], phase: str) -> list[dict[str, str]]:
    raw = str(state_side.get("status") or "")
    has_start = bool(trace.get("sessionId") or trace.get("eventCount") or raw in {"running", "staged", "clean"})
    has_thinking = bool(
        trace.get("assistantTurns")
        or trace.get("thinkingTokens")
        or trace.get("toolCalls")
        or raw in {"staged", "clean"}
    )
    has_tools = bool(trace.get("toolCalls") or raw in {"staged", "clean"})
    has_change = bool(trace.get("filesTouched") or trace.get("commands") or raw in {"staged", "clean"})
    has_finish = bool(trace.get("result") or raw in {"staged", "clean"})
    flags = [has_start, has_thinking, has_tools, has_change, has_finish]
    labels = ["启动", "思考", "工具", "改动", "收尾"]
    current = 0
    for index, flag in enumerate(flags):
        if flag:
            current = index
    if phase in {"thinking", "retrying"} and has_start:
        current = 1
    elif phase in {"tool", "tool_result"} and has_tools:
        current = 2
    elif phase in {"finishing", "staged", "clean"}:
        current = 4
    items: list[dict[str, str]] = []
    for index, (label, flag) in enumerate(zip(labels, flags)):
        if flag:
            item_state = "done"
        elif index == current:
            item_state = "current"
        else:
            item_state = "pending"
        items.append({"label": label, "state": item_state})
    return items


def _side_snapshot(
    task_root: Path,
    task_name: str,
    state: dict[str, Any],
    side: str,
    trace_cache: TraceCache,
    docker: dict[str, Any],
    stale_seconds: float,
    now: float,
    *,
    state_side_override: dict[str, Any] | None = None,
) -> dict[str, Any]:
    state_side = copy.deepcopy(
        state_side_override
        if state_side_override is not None
        else (state.get("sides") or {}).get(side) or {}
    )
    candidate_id = str(state_side.get("candidateId") or "")
    runtime_key = candidate_id if _is_candidate_id(candidate_id) else side
    attempts = _attempt_dirs(task_root, runtime_key)
    attempt_dir = attempts[-1] if attempts else None
    attempt = read_json(attempt_dir / "attempt.json", {}) if attempt_dir else {}
    if not isinstance(attempt, dict):
        attempt = {}
    stdout_path = attempt_dir / "stdout.jsonl" if attempt_dir else None
    trace = trace_cache.stats(stdout_path)
    try:
        trace_mtime = stdout_path.stat().st_mtime if stdout_path and stdout_path.exists() else 0.0
    except OSError:
        trace_mtime = 0.0
    container = _container_for_side(task_name, runtime_key, state_side, attempt, docker)
    runner_live = runner_pid_alive(state_side, task_root, side)
    container_running = str(container.get("state") or "") == "running"
    active = bool(runner_live or container_running)
    raw_status = str(state_side.get("status") or "")
    silent_seconds = max(0.0, now - trace_mtime) if trace_mtime else None
    if raw_status == "running" and not active and (silent_seconds is None or silent_seconds >= stale_seconds):
        is_stale = True
    else:
        is_stale = False
    phase, phase_detail = _phase_for_side(state_side, trace)
    if is_stale:
        phase, phase_detail = "stale", "执行器与容器均不存活，现场已静默"
    if raw_status not in {"running", "staged", "clean", "blocked", "attempt_invalid", "invalidated"}:
        if not attempt and not container and not trace.get("eventCount"):
            phase, phase_detail = "idle", "尚未启动"
    ever_started = bool(attempt or state_side or container or trace.get("eventCount"))
    can_resume = bool(
        side.upper() in SIDES
        and state.get("status") not in TERMINAL_TASK_STATUSES
        and raw_status not in SIDE_DONE_STATUSES
        and state.get("status") in RUNNABLE_TASK_STATUSES
    )
    started_at = str(attempt.get("startedAt") or state_side.get("startedAt") or "")
    elapsed = age_seconds(started_at, now=now)
    last_activity_at = iso_from_timestamp(trace_mtime) if trace_mtime else ""
    last_error = str(trace.get("lastError") or state_side.get("error") or "")
    if active and not last_error:
        health = "running"
    elif raw_status in SIDE_DONE_STATUSES:
        health = raw_status
    elif is_stale:
        health = "stale"
    elif raw_status in SIDE_FAILED_STATUSES:
        health = "failed"
    elif raw_status == "cancelled":
        health = "idle"
    elif raw_status == "running":
        health = "starting"
    else:
        health = "idle"
    container_snapshot = {
        "id": container.get("id", ""),
        "name": container.get("name", ""),
        "state": container.get("state", "missing"),
        "status": container.get("status", "not found"),
        "image": container.get("image", ""),
        "createdAt": container.get("createdAt", ""),
        "running": container_running,
    }
    if candidate_id:
        workspace_path = state_side.get("workspacePath") or (task_root / "source" / "candidates" / candidate_id)
    else:
        workspace_path = task_root / "source" / side.lower()
    return {
        "side": side,
        "status": raw_status or "idle",
        "phase": phase,
        "phaseDetail": phase_detail,
        "health": health,
        "stale": is_stale,
        "active": active,
        "runnerAlive": runner_live,
        "runnerPid": state_side.get("runPid"),
        "attempt": state_side.get("attempt") or attempt.get("attempt") or len(attempts),
        "attemptCount": len(attempts),
        "sessionId": str(attempt.get("sessionId") or trace.get("sessionId") or state_side.get("sessionId") or ""),
        "model": str(attempt.get("model") or trace.get("model") or ""),
        "harnessVersion": str(attempt.get("harnessVersion") or trace.get("harnessVersion") or ""),
        "startedAt": started_at,
        "elapsedSeconds": elapsed,
        "lastActivityAt": last_activity_at,
        "silentSeconds": silent_seconds,
        "everStarted": ever_started,
        "canResume": can_resume,
        "needsResume": bool(is_stale or raw_status in SIDE_FAILED_STATUSES),
        "container": container_snapshot,
        "trace": {
            key: trace.get(key)
            for key in (
                "eventCount",
                "assistantTurns",
                "toolCalls",
                "toolResults",
                "thinkingTokens",
                "apiRetries",
                "compactions",
                "commands",
                "filesTouched",
                "lastPhase",
                "lastSummary",
                "lastText",
                "lastTool",
                "lastResult",
                "lastError",
                "result",
                "sessionId",
                "todos",
                "todoUpdatedAt",
            )
        },
        "stages": _progress_stages(trace, state_side, phase),
        "events": list((trace.get("recent") or [])[-24:]),
        "history": [
            {
                key: item.get(key)
                for key in (
                    "key", "attempt", "source", "sources", "isCurrent", "status",
                    "sessionId", "startedAt", "finishedAt", "durationSeconds",
                    "container", "error", "rejectionReason", "tracePath", "stdoutPath", "stderrPath",
                )
            }
            | {
                "trace": {
                    key: (item.get("trace") or {}).get(key)
                    for key in ("eventCount", "toolCalls", "apiRetries", "lastSummary", "lastError", "result")
                }
            }
            for item in _attempt_history(task_root, side, state_side, trace_cache, docker)
        ],
        "workspace": str(workspace_path),
        "candidateId": candidate_id,
        "mappedSide": str(state_side.get("mappedSide") or ""),
        "completionOrder": state_side.get("completionOrder"),
        "attemptDir": str(attempt_dir or ""),
        "stdoutPath": str(stdout_path or ""),
    }


def _candidate_snapshot(
    task_root: Path,
    task_name: str,
    state: dict[str, Any],
    candidate: str,
    trace_cache: TraceCache,
    docker: dict[str, Any],
    stale_seconds: float,
    now: float,
) -> dict[str, Any]:
    record = copy.deepcopy((state.get("candidates") or {}).get(candidate) or {})
    record["candidateId"] = candidate
    snapshot = _side_snapshot(
        task_root,
        task_name,
        state,
        candidate,
        trace_cache,
        docker,
        stale_seconds,
        now,
        state_side_override=record,
    )
    snapshot["candidateId"] = candidate
    snapshot["mappedSide"] = str(record.get("mappedSide") or "")
    snapshot["completionOrder"] = record.get("completionOrder")
    return snapshot


def _review_completed(path: Path) -> bool:
    review = read_json(path, {})
    if not isinstance(review, dict):
        return False
    return bool(
        review.get("completed") is True
        and review.get("interrupted") is False
        and isinstance(review.get("unfinished"), list)
        and not review.get("unfinished")
    )


def _workflow_steps(task_root: Path, state: dict[str, Any], task: dict[str, Any]) -> dict[str, Any]:
    sides = task.get("sides") or {}
    statuses = {side: str((sides.get(side) or {}).get("status") or "idle") for side in SIDES}
    candidate_items = task.get("candidates") or []
    candidate_statuses = {
        str(item.get("candidateId") or ""): str(item.get("status") or "idle")
        for item in candidate_items
    }
    mapping = task.get("candidateMapping") if isinstance(task.get("candidateMapping"), dict) else {}
    done_statuses = SIDE_DONE_STATUSES
    both_run_done = all(statuses[side] in done_statuses for side in SIDES)
    candidate_race_done = bool(
        mapping.get("A", {}).get("candidateId")
        and mapping.get("B", {}).get("candidateId")
        and both_run_done
    )
    candidate_race_failed = any(status in {"blocked", "attempt_invalid"} for status in candidate_statuses.values())

    semantic_a = task_root / "monitor" / "semantic" / "a.review.json"
    semantic_b = task_root / "monitor" / "semantic" / "b.review.json"
    semantic_done = _review_completed(semantic_a) and _review_completed(semantic_b)

    publish_done = bool(
        str(state.get("status") or "") in {"ab_clean", "verified", "gsb_ready", "recorded", "complete"}
        and all((sides.get(side) or {}).get("artifactSnapshot") for side in SIDES)
    )
    evidence_path = task_root / "monitor" / "evidence.json"
    audit_path = task_root / "monitor" / "audit.json"
    audit_done = evidence_path.is_file() and audit_path.is_file()
    excel_path = task_root / "workspace" / "评审文件" / "交付表.xlsx"
    guide_path = task_root / "workspace" / "评审文件" / "GSB提交字段说明.md"
    gsb_done = excel_path.is_file() and guide_path.is_file()
    videos = {
        side: sorted((task_root / "workspace" / "视频信息" / side.lower() / "视频").glob("*.mp4"))
        for side in SIDES
    }
    recording_done = all(videos[side] for side in SIDES)
    complete_done = str(state.get("status") or "") == "complete"

    steps: list[dict[str, Any]] = []
    def add(
        key: str,
        label: str,
        done: bool,
        detail: str,
        *,
        blocked: bool = False,
        current: bool = False,
    ) -> None:
        steps.append({
            "key": key,
            "label": label,
            "done": bool(done),
            "blocked": bool(blocked),
            "currentHint": bool(current),
            "detail": detail,
        })

    source_ok = (task_root / "source" / "origin").is_dir() and bool(state.get("source") or state.get("sourcePath"))
    prompt_check = read_json(task_root / "monitor" / "prompt" / "prompt-check-r01.json", {})
    prompt_ok = bool(state.get("promptSha256")) and prompt_check.get("ok") is not False
    add("setup", "接入源码与提示词", source_ok and prompt_ok, "源码和唯一提示词已接入" if source_ok and prompt_ok else "等待源码或提示词校验")
    github_ok = bool(state.get("repoUrl") and state.get("initialSnapshot") and candidate_race_done)
    candidate_detail = "；".join(
        f"{item.get('candidateId')}={item.get('status')}"
        + (f"→{item.get('mappedSide')}" if item.get("mappedSide") else "")
        for item in candidate_items
    ) or "等待启动候选竞速"
    add(
        "candidates",
        "候选并行竞速并映射 A/B",
        candidate_race_done,
        candidate_detail,
        blocked=candidate_race_failed and not candidate_race_done,
        current=not candidate_race_done,
    )
    add(
        "github",
        "上传源码并初始化 GitHub main/A/B",
        github_ok,
        "候选映射后已创建公开仓库和三支" if github_ok else "等待前两名候选完成后建库",
        current=candidate_race_done and not github_ok,
    )
    add("review", "结构与语义完成审核", both_run_done and semantic_done, "A/B 审核文件均完成" if both_run_done and semantic_done else ("等待两侧结构校验与语义审核" if both_run_done else "等待候选映射与 A/B 审核"), current=both_run_done and not semantic_done)
    add("publish", "原子发布 A/B 产物", publish_done, "A/B 产物 commit 已发布" if publish_done else "等待双侧审核通过后发布", current=semantic_done and not publish_done)
    add("audit", "真实构建与运行验证", audit_done, "evidence.json / audit.json 已生成" if audit_done else "等待 verification-plan 与 audit", current=publish_done and not audit_done)
    add("gsb", "生成 GSB 交付表", gsb_done, "21 字段 Excel 与字段说明已生成" if gsb_done else "等待 audit 后生成 GSB 文案与交付表", current=audit_done and not gsb_done)
    add("record", "A/B 真实录屏", recording_done, f"A={len(videos['A'])} 个，B={len(videos['B'])} 个视频" if not recording_done else "A/B 视频均已生成", current=gsb_done and not recording_done)
    add("complete", "最终 status 复核", complete_done, "任务已标记 complete" if complete_done else "等待全部交付物由 status 复核", current=recording_done and not complete_done)

    first_open = next((index for index, step in enumerate(steps) if not step["done"]), None)
    for index, step in enumerate(steps):
        if step["done"]:
            step["state"] = "done"
        elif step["blocked"]:
            step["state"] = "blocked"
        elif index == first_open or step["currentHint"]:
            step["state"] = "current"
        else:
            step["state"] = "pending"
        step.pop("currentHint", None)
    done_count = sum(1 for step in steps if step["done"])
    return {
        "done": done_count,
        "total": len(steps),
        "percent": round(done_count / len(steps) * 100) if steps else 0,
        "steps": steps,
    }


def _submission_matches_task(item: dict[str, Any], task: dict[str, Any]) -> bool:
    repo_name = str(task.get("repoName") or "")
    repo_url = str(task.get("repoUrl") or "")
    project_code = str(task.get("projectCode") or "")
    sid = str(item.get("sessionId") or "")
    prompt = str(item.get("prompt") or "")
    repo = str(item.get("repo") or "")
    if repo_name and (repo_name in repo or repo_name in repo_url):
        return True
    if project_code and (project_code in repo or project_code in prompt):
        return True
    if sid and sid in {
        str((task.get("sides") or {}).get("A", {}).get("sessionId") or "")[:8],
        str((task.get("sides") or {}).get("B", {}).get("sessionId") or "")[:8],
    }:
        return True
    return False


def _public_submission(item: dict[str, Any]) -> dict[str, Any]:
    allowed = (
        "id",
        "at",
        "submittedAt",
        "who",
        "type",
        "round",
        "status",
        "statusLabel",
        "stage",
        "stageLabel",
        "repo",
        "sessionId",
        "avg",
        "prompt",
        "qc",
        "qcConclusion",
        "qcRunning",
        "qcFinishedAt",
        "lastTimelineAction",
        "lastTimelineAt",
        "currentVersion",
        "lark",
    )
    result = {key: item.get(key) for key in allowed if item.get(key) is not None}
    if result.get("prompt"):
        result["prompt"] = redact_text(result["prompt"], 180)
    if result.get("qc"):
        result["qc"] = redact_text(result["qc"], 240)
    return result


class SubmissionProvider:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.Lock()
        self._at = 0.0
        self._data: dict[str, Any] = {"items": [], "error": "", "source": ""}

    def get(self, force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            if not force and self._at and now - self._at < SUBS_TTL:
                return copy.deepcopy(self._data)
        data = self._fetch()
        with self._lock:
            self._at = time.time()
            self._data = data
            return copy.deepcopy(data)

    def _fetch(self) -> dict[str, Any]:
        cfg = self.config.get("solo2") or {}
        if not cfg.get("enabled", True):
            return {"items": [], "error": "", "source": "disabled", "fetchedAt": utc_now()}
        if cfg.get("preferMonitor", True):
            from_monitor = self._fetch_monitor(cfg)
            if not from_monitor.get("error"):
                return from_monitor
            fallback = self._fetch_direct(cfg)
            if not fallback.get("error"):
                fallback["fallbackReason"] = from_monitor.get("error", "")
                return fallback
            return {
                "items": [],
                "error": fallback.get("error") or from_monitor.get("error"),
                "source": "none",
                "fetchedAt": utc_now(),
            }
        return self._fetch_direct(cfg)

    def _fetch_monitor(self, cfg: dict[str, Any]) -> dict[str, Any]:
        base = str(cfg.get("monitorUrl") or "").rstrip("/")
        if not base:
            return {"items": [], "error": "未配置 solo2-monitor 地址", "source": "monitor"}
        size = int(cfg.get("pageSize") or DEFAULT_PAGE_SIZE)
        url = f"{base}/api/submissions?page_size={size}"
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"items": [], "error": f"solo2-monitor 未响应：{exc}", "source": "monitor"}
        items = data.get("items") if isinstance(data, dict) else []
        if not isinstance(items, list):
            items = []
        clean = dict(data) if isinstance(data, dict) else {}
        clean["items"] = [_public_submission(item) for item in items if isinstance(item, dict)]
        clean["source"] = "solo2-monitor"
        clean["fetchedAt"] = clean.get("fetchedAt") or utc_now()
        clean.setdefault("error", "")
        return clean

    @staticmethod
    def _keychain(service: str) -> str:
        try:
            result = subprocess.run(
                ["security", "find-generic-password", "-s", service, "-w"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            return result.stdout.strip() if result.returncode == 0 else ""
        except (OSError, subprocess.TimeoutExpired):
            return ""

    def _fetch_direct(self, cfg: dict[str, Any]) -> dict[str, Any]:
        cookie = self._keychain("solo2-jzxhnh-cookie")
        csrf = self._keychain("solo2-jzxhnh-csrf")
        if not cookie:
            return {
                "items": [],
                "error": "读不到 SOLO2 凭据（Keychain service: solo2-jzxhnh-cookie）",
                "source": "keychain",
            }
        base = str(cfg.get("apiBaseUrl") or "").rstrip("/")
        size = int(cfg.get("pageSize") or DEFAULT_PAGE_SIZE)
        headers = {"Cookie": cookie, "x-csrf-token": csrf, "Accept": "application/json"}

        def get(path: str) -> Any:
            request = urllib.request.Request(base + path, headers=headers)
            with urllib.request.urlopen(request, timeout=15) as response:
                return json.loads(response.read().decode("utf-8"))

        try:
            raw = get(f"/submissions?page=1&page_size={size}")
        except (OSError, ValueError, urllib.error.URLError) as exc:
            return {"items": [], "error": f"拉取提交列表失败：{exc}", "source": "keychain"}
        try:
            stats = get("/submissions/stats")
        except Exception:
            stats = None
        items: list[dict[str, Any]] = []
        for item in raw.get("items") or []:
            if not isinstance(item, dict):
                continue
            scores = item.get("scores") or {}
            values = [value for value in scores.values() if isinstance(value, (int, float))]
            normalized = {
                "id": item.get("id"),
                "at": item.get("submitted_at"),
                "submittedAt": item.get("submitted_at"),
                "who": item.get("submitter_name"),
                "type": item.get("question_type"),
                "round": item.get("round_no"),
                "status": item.get("status"),
                "statusLabel": item.get("status_label") or item.get("stage_label"),
                "stage": item.get("stage"),
                "stageLabel": item.get("stage_label"),
                "repo": item.get("repo_id"),
                "sessionId": str(item.get("session_id") or "")[:8],
                "avg": round(sum(values) / len(values), 2) if values else None,
                "prompt": item.get("prompt_excerpt") or "",
                "qc": item.get("qc_summary"),
                "qcConclusion": item.get("qc_conclusion"),
                "qcRunning": bool(item.get("qc_running")),
                "qcFinishedAt": item.get("qc_finished_at"),
                "currentVersion": item.get("current_version"),
                "lark": item.get("lark_sync_status"),
            }
            items.append(_public_submission(normalized))
        return {
            "items": items,
            "total": (raw.get("meta") or {}).get("total"),
            "stats": stats,
            "source": "keychain",
            "fetchedAt": utc_now(),
            "error": "",
        }


class JobManager:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._jobs: dict[str, dict[str, Any]] = {}
        self._recent: list[dict[str, Any]] = []
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._recover_running_jobs()

    def _recover_running_jobs(self) -> None:
        seen_keys: set[str] = set()
        candidates = sorted(
            (STATE_DIR / "jobs").glob("*.json"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in candidates:
            job = read_json(path, {})
            if not isinstance(job, dict):
                continue
            key = str(job.get("key") or "")
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            if job.get("status") != "running" or not persisted_job_process_alive(job):
                continue
            persisted = copy.deepcopy(job)
            persisted["logPath"] = str(persisted.get("logPath") or path.with_suffix(".log"))
            self._jobs[key] = persisted
            self._recent.append(persisted)

    @staticmethod
    def key(task_root: Path | str, side: str) -> str:
        return f"{Path(task_root).resolve()}::{str(side).upper()}"

    def get(self, task_root: Path | str, side: str) -> dict[str, Any] | None:
        with self._lock:
            item = self._jobs.get(self.key(task_root, side))
            return copy.deepcopy(item) if item else None

    def running(self) -> list[dict[str, Any]]:
        with self._lock:
            return [copy.deepcopy(item) for item in self._jobs.values() if item.get("status") == "running"]

    def start(
        self,
        task_root: Path,
        side: str,
        *,
        force: bool = False,
        reason: str = "manual",
    ) -> dict[str, Any]:
        side = str(side).upper()
        if side not in {"A", "B", "BOTH"}:
            raise MonitorError("side 只能为 A、B 或 both")
        key = self.key(task_root, side)
        with self._lock:
            current = self._jobs.get(key)
            if current and current.get("status") == "running":
                raise MonitorError(f"{side} 已有监控端任务在运行，PID={current.get('pid')}")
            script = Path(str(self.config.get("skillScript") or DEFAULT_SKILL_SCRIPT)).expanduser().resolve()
            if not script.is_file():
                raise MonitorError(f"找不到 sologsb CLI：{script}")
            command = [
                sys.executable,
                str(script),
                "run",
                "--task-root",
                str(task_root.resolve()),
                "--side",
                side.lower() if side == "BOTH" else side,
            ]
            if force:
                command.append("--force")
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            log_path = STATE_DIR / "jobs" / f"{stamp}-{safe_slug(task_root.name)}-{side.lower()}.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            log_handle = log_path.open("ab", buffering=0)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(task_root),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except Exception:
                log_handle.close()
                raise
            job = {
                "key": key,
                "taskRoot": str(task_root.resolve()),
                "taskName": task_root.name,
                "side": side,
                "status": "running",
                "reason": reason,
                "force": force,
                "pid": process.pid,
                "startedAt": utc_now(),
                "finishedAt": "",
                "exitCode": None,
                "command": " ".join(shlex.quote(part) for part in command),
                "logPath": str(log_path),
            }
            self._jobs[key] = job
            self._recent.append(job)
            del self._recent[:-40]
            self._persist(job)
            thread = threading.Thread(
                target=self._wait,
                args=(key, process, log_handle),
                name=f"job-{side.lower()}-{process.pid}",
                daemon=True,
            )
            thread.start()
            return copy.deepcopy(job)

    def get_platform(self, item_id: str) -> dict[str, Any] | None:
        with self._lock:
            key = f"platform:{item_id}"

            def finalize_dead_job(job: dict[str, Any]) -> dict[str, Any]:
                result_file = Path(str(job.get("resultFile") or ""))
                result = read_json(result_file, {}) if result_file else {}
                if isinstance(result, dict) and result.get("status") == "finished":
                    job["status"] = "finished"
                    job["exitCode"] = int(result.get("exitCode") or 0)
                else:
                    job["status"] = "failed"
                    job["exitCode"] = int((result or {}).get("exitCode") or -1)
                job["finishedAt"] = utc_now()
                if job["status"] == "failed":
                    job["error"] = str((result or {}).get("error") or "监控重启后发现执行器进程已退出")
                return job

            item = self._jobs.get(key)
            if item is not None and item.get("status") == "running" and not persisted_job_process_alive(item):
                item = finalize_dead_job(item)
                log_path = Path(str(item.get("logPath") or ""))
                if log_path:
                    try:
                        atomic_write_json(log_path.with_suffix(".json"), item)
                    except OSError:
                        pass
            if item is None:
                pattern = f"*-platform-{safe_slug(item_id)}.json"
                candidates = sorted(
                    (STATE_DIR / "jobs").glob(pattern),
                    key=lambda path: path.stat().st_mtime if path.exists() else 0,
                    reverse=True,
                )
                for path in candidates:
                    persisted = read_json(path, {})
                    if not isinstance(persisted, dict) or persisted.get("key") != key:
                        continue
                    if persisted.get("status") == "running" and not persisted_job_process_alive(persisted):
                        persisted = finalize_dead_job(persisted)
                        try:
                            atomic_write_json(path, persisted)
                        except OSError:
                            pass
                    item = persisted
                    self._jobs[key] = persisted
                    break
            return copy.deepcopy(item) if item else None

    def start_platform(self, item: dict[str, Any], *, reason: str = "queue") -> dict[str, Any]:
        item_id = str(item.get("id") or "")
        if not item_id:
            raise MonitorError("平台队列项缺少 id")
        key = f"platform:{item_id}"
        with self._lock:
            current = self._jobs.get(key)
            if current and current.get("status") == "running":
                raise MonitorError(f"平台任务已在运行，PID={current.get('pid')}")
            script = Path(str(self.config.get("skillScript") or DEFAULT_SKILL_SCRIPT)).expanduser().resolve()
            worker = APP_DIR / "queue_worker.py"
            if not script.is_file():
                raise MonitorError(f"找不到 sologsb CLI：{script}")
            if not worker.is_file():
                raise MonitorError(f"找不到队列 worker：{worker}")
            push_helper = str((self.config.get("automation") or {}).get("codexQueuePush") or "").strip()
            roots = [Path(value).expanduser().resolve() for value in self.config.get("roots") or [DEFAULT_ROOT]]
            workdir = roots[0] if roots else DEFAULT_ROOT
            workdir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            task_name = f"{item.get('projectCode') or 'platform'}-{stamp}"
            result_file = STATE_DIR / "jobs" / f"{stamp}-platform-{safe_slug(item_id)}.result.json"
            log_path = STATE_DIR / "jobs" / f"{stamp}-platform-{safe_slug(item_id)}.log"
            trigger_prompt = str(item.get("triggerPrompt") or "")
            trigger_prompt_path: Path | None = STATE_DIR / "jobs" / f"{stamp}-platform-{safe_slug(item_id)}.prompt.txt"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if trigger_prompt:
                trigger_prompt_path.write_text(trigger_prompt + "\n", encoding="utf-8")
            else:
                trigger_prompt_path = None
            command = [
                sys.executable,
                str(worker),
                "--skill-script", str(script),
                "--workdir", str(workdir),
                "--task-name", task_name,
                "--project-code", str(item.get("projectCode") or ""),
                "--task-type", str(item.get("taskType") or "0-1代码生成"),
                "--difficulty", str(item.get("difficulty") or "困难"),
                "--side", str(item.get("side") or "both"),
                "--result-file", str(result_file),
            ]
            if push_helper:
                command.extend(["--push-helper", push_helper])
            if trigger_prompt_path:
                command.extend(["--trigger-prompt-file", str(trigger_prompt_path)])
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            log_handle = log_path.open("ab", buffering=0)
            try:
                process = subprocess.Popen(
                    command,
                    cwd=str(workdir),
                    stdout=log_handle,
                    stderr=subprocess.STDOUT,
                    stdin=subprocess.DEVNULL,
                    env=env,
                    start_new_session=True,
                )
            except Exception:
                log_handle.close()
                raise
            job = {
                "key": key,
                "source": "platform",
                "platformItemId": item_id,
                "taskRoot": str(item.get("taskRoot") or ""),
                "taskName": str(item.get("projectName") or item.get("projectCode") or task_name),
                "projectCode": str(item.get("projectCode") or ""),
                "side": str(item.get("side") or "both"),
                "status": "running",
                "reason": reason,
                "force": False,
                "pid": process.pid,
                "startedAt": utc_now(),
                "finishedAt": "",
                "exitCode": None,
                "command": " ".join(shlex.quote(part) for part in command),
                "logPath": str(log_path),
                "resultFile": str(result_file),
                "triggerPromptPath": str(trigger_prompt_path) if trigger_prompt_path else "",
            }
            self._jobs[key] = job
            self._recent.append(job)
            del self._recent[:-40]
            self._persist(job)
            thread = threading.Thread(
                target=self._wait,
                args=(key, process, log_handle),
                name=f"job-platform-{process.pid}",
                daemon=True,
            )
            thread.start()
            return copy.deepcopy(job)

    def _persist(self, job: dict[str, Any]) -> None:
        log_path = Path(str(job.get("logPath") or ""))
        if not log_path:
            return
        try:
            atomic_write_json(log_path.with_suffix(".json"), job)
        except OSError:
            pass

    def _wait(self, key: str, process: subprocess.Popen, log_handle) -> None:
        try:
            code = process.wait()
        finally:
            try:
                log_handle.close()
            except OSError:
                pass
        with self._lock:
            job = self._jobs.get(key)
            if job:
                job["status"] = "finished" if code == 0 else "failed"
                job["exitCode"] = code
                job["finishedAt"] = utc_now()
                started = parse_time(job.get("startedAt"))
                job["durationSeconds"] = max(
                    0.0,
                    time.time() - (started.timestamp() if started else time.time()),
                )
                self._persist(job)

    def _latest_persisted_job(self, task_root: Path, side: str) -> dict[str, Any] | None:
        pattern = f"*-{safe_slug(task_root.name)}-{str(side).lower()}.json"
        candidates = sorted(
            (STATE_DIR / "jobs").glob(pattern),
            key=lambda item: item.stat().st_mtime if item.exists() else 0,
            reverse=True,
        )
        for candidate in candidates:
            item = read_json(candidate, {})
            if isinstance(item, dict) and item:
                return item
        return None

    def tail_log(self, task_root: Path, side: str, lines: int = 200) -> dict[str, Any]:
        job = self.get(task_root, side) or self._latest_persisted_job(task_root, side)
        path = Path(job["logPath"]) if job and job.get("logPath") else None
        if path is None or not path.is_file():
            return {"job": job, "lines": []}
        try:
            content = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            content = []
        return {"job": job, "lines": [redact_text(line, 1200) for line in content[-max(1, min(lines, 1000)):]]}


class PlatformProvider:
    """Read-only Solo Manager project candidate provider."""

    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._lock = threading.RLock()
        self._cache: dict[str, dict[str, Any]] = {}

    def _modules(self):
        for scripts in (PLATFORM_SCRIPTS, DEFAULT_SKILL_SCRIPT.parent):
            if str(scripts) not in sys.path:
                sys.path.insert(0, str(scripts))
        try:
            import platform_bridge  # type: ignore
            import project_claims  # type: ignore
        except Exception as exc:
            raise MonitorError(f"无法加载 Solo Manager 适配器: {exc}") from exc
        return platform_bridge, project_claims

    @staticmethod
    def _project_code(project: dict[str, Any]) -> str:
        return str(project.get("code") or "").strip()

    @staticmethod
    def _usable_variant(project: dict[str, Any]) -> dict[str, Any] | None:
        readiness = str(project.get("readinessStatus") or "").strip().upper()
        if project.get("disabled") or (readiness and readiness != "RUNNABLE"):
            return None
        variants = [
            item for item in (project.get("variants") or [])
            if isinstance(item, dict) and item.get("sourceAvailable") and item.get("sourceAsset")
        ]
        variants.sort(key=lambda item: str(item.get("directoryName") or item.get("id") or ""))
        return variants[0] if variants else None

    @staticmethod
    def _quota(project: dict[str, Any], task_type: str) -> dict[str, Any] | None:
        for quota in project.get("quotas") or []:
            if isinstance(quota, dict) and str(quota.get("taskType") or "") == task_type:
                return quota
        return None

    def candidates(self, task_type: str = "0-1代码生成", force: bool = False) -> dict[str, Any]:
        now = time.time()
        with self._lock:
            cached = self._cache.get(task_type)
            ttl = float((self.config.get("platform") or {}).get("candidateTtlSeconds") or 30)
            if cached and not force and now - float(cached.get("_at") or 0) < ttl:
                return copy.deepcopy(cached)
        pb, claims = self._modules()
        cfg = self.config.get("platform") or {}
        base_url = str(cfg.get("managerBaseUrl") or "").strip().rstrip("/")
        if not base_url:
            raise MonitorError("未配置 Solo Manager 地址，请设置 SOLO_MANAGER_BASE_URL 或 config.json 的 platform.managerBaseUrl")
        meta = {"baseUrl": base_url, "apiBaseUrl": base_url + "/api/v1"}
        token = pb.load_manager_token()
        if not token:
            raise MonitorError("Solo Manager 未登录，找不到 manager token")
        workdir = None
        roots = self.config.get("roots") or []
        if roots:
            workdir = Path(str(roots[0]))
        try:
            running_codes, running_source = claims.running_container_project_codes(workdir)
            claim_codes = claims.claimed_project_codes(base_url)
        except Exception as exc:
            raise MonitorError(f"读取项目占用状态失败: {exc}") from exc
        active_codes = set(running_codes) | set(claim_codes)
        items: list[dict[str, Any]] = []
        seen: set[str] = set()
        excluded: list[dict[str, str]] = []
        errors: list[str] = []
        for stage, path in (("我的项目", "/projects/mine?page=1&size=200"), ("项目池", "/projects?page=1&size=200")):
            try:
                payload = pb.api_json(meta, path, token=token)
            except Exception as exc:
                errors.append(f"{stage}: {exc}")
                continue
            for project in payload.get("items") or []:
                if not isinstance(project, dict):
                    continue
                code = self._project_code(project)
                identity = code.casefold() or str(project.get("id") or "")
                if not identity or identity in seen:
                    continue
                seen.add(identity)
                if code and code.casefold() in {str(value).casefold() for value in active_codes}:
                    excluded.append({"code": code, "reason": "已占用或运行中"})
                    continue
                variant = self._usable_variant(project)
                if variant is None:
                    excluded.append({"code": code or identity, "reason": "缺少可用源码快照"})
                    continue
                quota = self._quota(project, task_type)
                if quota is None:
                    excluded.append({"code": code or identity, "reason": f"没有 {task_type} 配额"})
                    continue
                if int(quota.get("remaining") or 0) <= 0:
                    excluded.append({"code": code or identity, "reason": "配额已耗尽"})
                    continue
                items.append({
                    "id": str(project.get("id") or ""),
                    "code": code,
                    "name": str(project.get("name") or ""),
                    "businessDomain": str(project.get("businessDomain") or ""),
                    "category": str(project.get("category") or ""),
                    "readinessStatus": str(project.get("readinessStatus") or ""),
                    "variantId": str(variant.get("id") or ""),
                    "variantName": str(variant.get("directoryName") or ""),
                    "languages": str(variant.get("languages") or ""),
                    "summary": str(variant.get("summary") or ""),
                    "sourceAssetId": str((variant.get("sourceAsset") or {}).get("id") or ""),
                    "sourceAssetSha256": str((variant.get("sourceAsset") or {}).get("sha256") or ""),
                    "quotaBefore": quota,
                    "stage": stage,
                })
        items.sort(key=lambda item: (str(item.get("code") or ""), str(item.get("name") or "")))
        result = {
            "items": items,
            "total": len(items),
            "excluded": excluded[:200],
            "errors": errors,
            "baseUrl": base_url,
            "taskType": task_type,
            "runningContainerSource": running_source,
            "fetchedAt": utc_now(),
            "_at": now,
        }
        with self._lock:
            self._cache[task_type] = result
        return copy.deepcopy(result)


class QueueManager:
    """Persistent task queue with capacity-limited automatic execution."""

    def __init__(self, config: dict[str, Any], jobs: JobManager, state_path: Path | None = None):
        self.config = config
        self.jobs = jobs
        self.state_path = state_path or QUEUE_STATE_PATH
        self._lock = threading.RLock()
        self._triggered: list[dict[str, Any]] = []
        self._lastStartedAt = ""
        self._items = self._load()
        self._recover_triggered()

    def _load(self) -> list[dict[str, Any]]:
        raw = read_json(self.state_path, {})
        triggered = raw.get("triggered") if isinstance(raw, dict) else []
        self._triggered = [item for item in triggered if isinstance(item, dict)] if isinstance(triggered, list) else []
        self._lastStartedAt = str(raw.get("lastStartedAt") or "") if isinstance(raw, dict) else ""
        items = raw.get("items") if isinstance(raw, dict) else []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)]

    def _save(self) -> None:
        atomic_write_json(
            self.state_path,
            {
                "items": self._items,
                "triggered": self._triggered[-200:],
                "lastStartedAt": self._lastStartedAt,
                "updatedAt": utc_now(),
            },
        )

    def _recover_triggered(self) -> None:
        known = {str(item.get("id") or "") for item in self._triggered}
        added = False
        roots = [Path(value).expanduser().resolve() for value in self.config.get("roots") or []]
        workdir = roots[0] if roots else DEFAULT_ROOT
        candidates = sorted(
            (STATE_DIR / "jobs").glob("*-platform-*.json"),
            key=lambda path: path.stat().st_mtime if path.exists() else 0,
            reverse=True,
        )
        for path in candidates:
            job = read_json(path, {})
            if not isinstance(job, dict):
                continue
            item_id = str(job.get("platformItemId") or "")
            if not item_id or item_id in known:
                continue
            result = read_json(Path(str(job.get("resultFile") or "")), {})
            if not isinstance(result, dict) or result.get("stage") not in {"desktop-submitted", "desktop-task-running"}:
                continue
            task_root = Path(str(result.get("taskRoot") or job.get("taskRoot") or ""))
            task_name = task_root.name
            trigger_path = Path(str(job.get("triggerPromptPath") or ""))
            if not task_name or not trigger_path.is_file():
                continue
            trigger_prompt = trigger_path.read_text(encoding="utf-8").strip()
            prompt = (
                "本次监控队列已分配唯一任务名。\n"
                f"- 监控工作目录：`{workdir}`\n"
                f"- 唯一任务名：`{task_name}`\n"
                "- 必须使用该任务名创建独立目录，不得复用已存在目录。\n\n"
                f"{trigger_prompt}\n"
            )
            self._triggered.append({
                "id": item_id,
                "source": "platform",
                "taskRoot": str(task_root),
                "taskName": task_name,
                "projectCode": str(job.get("projectCode") or ""),
                "triggerPrompt": trigger_prompt,
                "promptSha256": str(result.get("promptSha256") or queue_prompt_sha256(prompt)),
                "triggeredAt": utc_now(),
                "removedReason": "triggered",
            })
            known.add(item_id)
            added = True
        self._triggered = self._triggered[-200:]
        if added:
            self._save()

    def triggered_items(self) -> list[dict[str, Any]]:
        with self._lock:
            return copy.deepcopy(self._triggered)

    def _automation_cfg(self) -> dict[str, Any]:
        return self.config.setdefault("automation", {})

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            self._sync_running_locked()
            items = copy.deepcopy(self._items)
        running = len(self.jobs.running())
        pending = sum(1 for item in items if item.get("status") == "pending")
        active = sum(1 for item in items if item.get("status") == "running")
        cooldown_seconds = max(0, int(self._automation_cfg().get("cooldownSeconds") or 0))
        started_at = parse_time(self._lastStartedAt)
        cooldown_remaining = (
            max(0.0, cooldown_seconds - (time.time() - started_at.timestamp()))
            if started_at and cooldown_seconds
            else 0.0
        )
        return {
            "roots": list(self.config.get("roots") or []),
            "capacity": int(self._automation_cfg().get("capacity") or 2),
            "cooldownSeconds": cooldown_seconds,
            "cooldownRemainingSeconds": round(cooldown_remaining, 1),
            "lastStartedAt": self._lastStartedAt,
            "paused": bool(self._automation_cfg().get("paused", True)),
            "promptTemplate": str(self._automation_cfg().get("promptTemplate") or DEFAULT_AUTO_TRIGGER_PROMPT),
            "items": items,
            "counts": {
                "pending": pending,
                "running": active,
                "done": sum(1 for item in items if item.get("status") == "done"),
                "failed": sum(1 for item in items if item.get("status") == "failed"),
                "skipped": sum(1 for item in items if item.get("status") == "skipped"),
                "jobsRunning": running,
            },
            "updatedAt": utc_now(),
        }

    def add(self, task_root: Path, side: str = "both") -> dict[str, Any]:
        task_root = task_root.expanduser().resolve()
        if not (task_root / "monitor" / "state.json").is_file():
            raise MonitorError(f"不是有效的 sologsb 任务目录: {task_root}")
        side = str(side).upper()
        if side == "BOTH":
            side = "both"
        if side not in {"A", "B", "both"}:
            raise MonitorError("队列 side 只能为 A、B 或 both")
        with self._lock:
            if any(
                Path(item.get("taskRoot") or "").resolve() == task_root
                and str(item.get("side") or "").lower() == side.lower()
                and item.get("status") in {"pending", "running"}
                for item in self._items
            ):
                raise MonitorError("该任务和侧已经在队列中")
            state = read_json(task_root / "monitor" / "state.json", {})
            item = {
                "id": f"{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                "taskRoot": str(task_root),
                "taskName": str(state.get("taskName") or task_root.name),
                "projectCode": "",
                "side": side,
                "status": "pending",
                "addedAt": utc_now(),
                "startedAt": "",
                "finishedAt": "",
                "jobPid": "",
                "error": "",
            }
            self._items.append(item)
            self._save()
            return copy.deepcopy(item)

    def add_platform(
        self,
        project: dict[str, Any],
        *,
        task_type: str = "0-1代码生成",
        difficulty: str = "困难",
        side: str = "both",
        trigger_prompt: str = "",
    ) -> dict[str, Any]:
        project_code = str(project.get("code") or "").strip()
        if not project_code:
            raise MonitorError("平台项目缺少 code")
        side = str(side).lower()
        if side not in {"a", "b", "both"}:
            raise MonitorError("队列 side 只能为 A、B 或 both")
        side = side.upper() if side in {"a", "b"} else "both"
        with self._lock:
            if any(
                item.get("source") == "platform"
                and str(item.get("projectCode") or "").casefold() == project_code.casefold()
                and str(item.get("taskType") or "") == task_type
                and item.get("status") in {"pending", "running"}
                for item in self._items
            ):
                raise MonitorError(f"项目 {project_code} / {task_type} 已在队列中")
            item = {
                "id": f"platform-{int(time.time() * 1000)}-{uuid.uuid4().hex[:8]}",
                "source": "platform",
                "taskRoot": "",
                "taskName": str(project.get("name") or project_code),
                "projectId": str(project.get("id") or ""),
                "projectCode": project_code,
                "projectName": str(project.get("name") or ""),
                "businessDomain": str(project.get("businessDomain") or ""),
                "category": str(project.get("category") or ""),
                "variantId": str(project.get("variantId") or ""),
                "variantName": str(project.get("variantName") or ""),
                "taskType": task_type,
                "difficulty": difficulty,
                "side": side,
                "triggerPrompt": trigger_prompt,
                "status": "pending",
                "addedAt": utc_now(),
                "startedAt": "",
                "finishedAt": "",
                "jobPid": "",
                "error": "",
            }
            self._items.append(item)
            self._save()
            return copy.deepcopy(item)

    def remove(self, item_id: str) -> None:
        with self._lock:
            for index, item in enumerate(self._items):
                if item.get("id") == item_id:
                    if item.get("status") == "running":
                        raise MonitorError("运行中的队列项不能直接删除，请先等待结束或暂停队列")
                    self._items.pop(index)
                    self._save()
                    return
        raise MonitorError("队列项不存在")

    def move(self, item_id: str, delta: int) -> None:
        with self._lock:
            index = next((i for i, item in enumerate(self._items) if item.get("id") == item_id), -1)
            if index < 0:
                raise MonitorError("队列项不存在")
            target = max(0, min(len(self._items) - 1, index + int(delta)))
            if target == index:
                return
            item = self._items.pop(index)
            self._items.insert(target, item)
            self._save()

    def retry(self, item_id: str) -> None:
        with self._lock:
            item = next((value for value in self._items if value.get("id") == item_id), None)
            if not item:
                raise MonitorError("队列项不存在")
            if item.get("status") == "running":
                raise MonitorError("运行中的队列项不能重试")
            item.update({
                "status": "pending",
                "startedAt": "",
                "finishedAt": "",
                "jobPid": "",
                "error": "",
            })
            self._save()

    def clear_finished(self) -> None:
        with self._lock:
            self._items = [item for item in self._items if item.get("status") not in {"done", "failed", "skipped"}]
            self._save()

    def set_paused(self, paused: bool) -> dict[str, Any]:
        self._automation_cfg()["paused"] = bool(paused)
        config_path = Path(str(self.config.get("_configPath") or CONFIG_PATH))
        save_config(self.config, config_path)
        return self.snapshot()

    def set_capacity(self, capacity: int) -> dict[str, Any]:
        value = int(capacity)
        if value < 1 or value > 20:
            raise MonitorError("并发数必须在 1 到 20 之间")
        self._automation_cfg()["capacity"] = value
        config_path = Path(str(self.config.get("_configPath") or CONFIG_PATH))
        save_config(self.config, config_path)
        return self.snapshot()

    def set_cooldown(self, seconds: int) -> dict[str, Any]:
        value = int(seconds)
        if value < 0 or value > 86400:
            raise MonitorError("任务冷却时间必须在 0 到 86400 秒之间")
        self._automation_cfg()["cooldownSeconds"] = value
        config_path = Path(str(self.config.get("_configPath") or CONFIG_PATH))
        save_config(self.config, config_path)
        return self.snapshot()

    def set_roots(self, roots: list[str]) -> dict[str, Any]:
        resolved: list[str] = []
        for raw in roots:
            path = Path(str(raw)).expanduser().resolve()
            if not path.is_dir():
                raise MonitorError(f"监控目录不存在: {path}")
            if str(path) not in resolved:
                resolved.append(str(path))
        if not resolved:
            raise MonitorError("至少保留一个监控目录")
        self.config["roots"] = resolved
        config_path = Path(str(self.config.get("_configPath") or CONFIG_PATH))
        save_config(self.config, config_path)
        return self.snapshot()

    @staticmethod
    def _task_state(task_root: Path) -> dict[str, Any]:
        state = read_json(task_root / "monitor" / "state.json", {})
        return state if isinstance(state, dict) else {}

    def _side_done(self, task_root: Path, side: str) -> bool:
        state = self._task_state(task_root)
        sides = state.get("sides") or {}
        if side == "both":
            return all(str((sides.get(name) or {}).get("status") or "") in SIDE_DONE_STATUSES for name in SIDES)
        return str((sides.get(side) or {}).get("status") or "") in SIDE_DONE_STATUSES

    def _side_active(self, task_root: Path, side: str) -> bool:
        state = self._task_state(task_root)
        sides = state.get("sides") or {}
        names = SIDES if side == "both" else (side,)
        for name in names:
            record = sides.get(name) or {}
            if str(record.get("status") or "") == "running" and runner_pid_alive(record, task_root, name):
                return True
        return False

    def _sync_running_locked(self) -> None:
        changed = False
        remove_ids: set[str] = set()
        triggered_ids: set[str] = set()
        for item in list(self._items):
            item_id = str(item.get("id") or "")
            if item.get("status") == "done":
                if item_id:
                    remove_ids.add(item_id)
                changed = True
                continue
            if item.get("source") == "platform":
                job = self.jobs.get_platform(item_id)
                result_file = Path(str((job or {}).get("resultFile") or ""))
                result = read_json(result_file, {}) if result_file else {}
                if (
                    isinstance(result, dict)
                    and result.get("taskRoot")
                    and result.get("stage") in {"desktop-submitted", "desktop-task-running"}
                ):
                    if item_id:
                        remove_ids.add(item_id)
                        triggered_ids.add(item_id)
                        item["triggeredAt"] = item.get("triggeredAt") or utc_now()
                        item["promptSha256"] = str(result.get("promptSha256") or item.get("promptSha256") or "")
                    changed = True
                    continue
                if item.get("status") == "pending" and job and job.get("status") == "running":
                    item["status"] = "running"
                    item["startedAt"] = job.get("startedAt") or item.get("startedAt") or utc_now()
                    item["finishedAt"] = ""
                    item["jobPid"] = job.get("pid")
                    item["error"] = ""
                    changed = True
                if item.get("status") != "running":
                    continue
                if job and job.get("status") == "running":
                    if item.get("jobPid") != job.get("pid"):
                        item["jobPid"] = job.get("pid")
                        changed = True
                    if isinstance(result, dict) and result.get("taskRoot") and item.get("taskRoot") != result.get("taskRoot"):
                        item["taskRoot"] = result.get("taskRoot")
                        changed = True
                    continue
                if job and job.get("status") in {"finished", "failed"}:
                    item["status"] = "done" if job.get("status") == "finished" else "failed"
                    item["error"] = "" if job.get("status") == "finished" else f"执行器退出码 {job.get('exitCode')}"
                    if job.get("status") == "finished" and item_id:
                        remove_ids.add(item_id)
                else:
                    item["status"] = "pending"
                    item["error"] = "监控服务重启或平台任务执行器已退出，已重新排队"
                item["finishedAt"] = utc_now() if item.get("status") in {"done", "failed"} else ""
                item["jobPid"] = ""
                changed = True
                continue
            if item.get("status") != "running":
                continue
            task_root = Path(str(item.get("taskRoot") or ""))
            side = str(item.get("side") or "both")
            job = self.jobs.get(task_root, side)
            if job and job.get("status") == "running":
                if item.get("jobPid") != job.get("pid"):
                    item["jobPid"] = job.get("pid")
                    changed = True
                continue
            if job and job.get("status") in {"finished", "failed"}:
                item["status"] = "done" if job.get("status") == "finished" else "failed"
                item["error"] = "" if job.get("status") == "finished" else f"执行器退出码 {job.get('exitCode')}"
            elif self._side_done(task_root, side):
                item["status"] = "done"
            elif self._side_active(task_root, side):
                continue
            else:
                item["status"] = "pending"
                item["error"] = "监控服务重启或执行器已退出，已重新排队"
            item["finishedAt"] = utc_now() if item.get("status") in {"done", "failed"} else ""
            item["jobPid"] = ""
            changed = True
        if remove_ids:
            for archived in self._items:
                if str(archived.get("id") or "") in triggered_ids:
                    previous = next(
                        (value for value in self._triggered if value.get("id") == archived.get("id")),
                        None,
                    )
                    if previous is None:
                        self._triggered.append(copy.deepcopy(archived))
                    else:
                        previous.update(copy.deepcopy(archived))
            self._items = [item for item in self._items if str(item.get("id") or "") not in remove_ids]
        if changed:
            self._save()

    def tick(self) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        with self._lock:
            self._sync_running_locked()
            if bool(self._automation_cfg().get("paused", True)):
                return actions
            capacity = int(self._automation_cfg().get("capacity") or 2)
            cooldown_seconds = max(0, int(self._automation_cfg().get("cooldownSeconds") or 0))
            last_started = parse_time(self._lastStartedAt)
            if last_started and cooldown_seconds and time.time() - last_started.timestamp() < cooldown_seconds:
                return actions
            running_jobs = self.jobs.running()
            if len(running_jobs) >= capacity:
                return actions
            for item in self._items:
                if len(running_jobs) >= capacity:
                    break
                if item.get("status") != "pending":
                    continue
                if item.get("source") == "platform":
                    if not str(item.get("projectCode") or "").strip():
                        item["status"] = "failed"
                        item["error"] = "平台项目缺少 projectCode"
                        item["finishedAt"] = utc_now()
                        continue
                    try:
                        job = self.jobs.start_platform(item, reason="queue")
                    except MonitorError as exc:
                        item["error"] = str(exc)
                        continue
                    item.update({
                        "status": "running",
                        "startedAt": utc_now(),
                        "jobPid": job.get("pid"),
                        "error": "",
                    })
                    self._lastStartedAt = utc_now()
                    actions.append({"item": copy.deepcopy(item), "job": job})
                    break
                task_root = Path(str(item.get("taskRoot") or ""))
                side = str(item.get("side") or "both")
                if not (task_root / "monitor" / "state.json").is_file():
                    item["status"] = "failed"
                    item["error"] = "任务目录不存在"
                    item["finishedAt"] = utc_now()
                    continue
                if self._side_done(task_root, side):
                    item["status"] = "skipped"
                    item["error"] = "目标侧已完成，无需执行"
                    item["finishedAt"] = utc_now()
                    continue
                if self._side_active(task_root, side):
                    continue
                try:
                    job = self.jobs.start(task_root, side, force=False, reason="queue")
                except MonitorError as exc:
                    # Another monitor job or a changed state may be transient; leave it pending.
                    item["error"] = str(exc)
                    continue
                item.update({
                    "status": "running",
                    "startedAt": utc_now(),
                    "jobPid": job.get("pid"),
                    "error": "",
                })
                self._lastStartedAt = utc_now()
                actions.append({"item": copy.deepcopy(item), "job": job})
                break
            self._save()
        return actions


class MonitorService:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.trace_cache = TraceCache(int((config.get("monitor") or {}).get("traceMaxBytes") or 16 * 1024 * 1024))
        self.docker_cache = DockerCache(float((config.get("monitor") or {}).get("dockerCacheSeconds") or 2.0))
        self.submissions = SubmissionProvider(config)
        self.jobs = JobManager(config)
        self.queue = QueueManager(config, self.jobs)
        self.platform = PlatformProvider(config)
        self._lock = threading.RLock()
        self._auto_lock = threading.RLock()
        self._auto = self._load_auto()
        self._dismissed = self._load_dismissed_tasks()

    def _load_dismissed_tasks(self) -> dict[str, dict[str, Any]]:
        raw = read_json(DISMISSED_TASKS_PATH, {})
        items = raw.get("items") if isinstance(raw, dict) else raw
        if isinstance(items, list):
            return {str(task_id): {"dismissedAt": ""} for task_id in items if str(task_id)}
        if not isinstance(items, dict):
            return {}
        return {
            str(task_id): copy.deepcopy(value) if isinstance(value, dict) else {}
            for task_id, value in items.items()
            if str(task_id)
        }

    def _save_dismissed_tasks_locked(self) -> None:
        atomic_write_json(DISMISSED_TASKS_PATH, {"items": copy.deepcopy(self._dismissed), "updatedAt": utc_now()})

    def _load_auto(self) -> dict[str, Any]:
        raw = read_json(AUTO_STATE_PATH, {})
        if not isinstance(raw, dict):
            raw = {}
        return deep_merge(
            {
                "globalEnabled": bool(((self.config.get("monitor") or {}).get("autoResume") or {}).get("enabled")),
                "tasks": {},
                "history": {},
                "lastRunAt": {},
                "log": [],
            },
            raw,
        )

    def _save_auto(self) -> None:
        atomic_write_json(AUTO_STATE_PATH, self._auto)

    def set_auto(self, enabled: bool, task_root: Path | None = None) -> dict[str, Any]:
        with self._auto_lock:
            if task_root is None:
                self._auto["globalEnabled"] = bool(enabled)
            else:
                key = str(task_root.resolve())
                tasks = self._auto.setdefault("tasks", {})
                tasks[key] = {
                    "enabled": bool(enabled),
                    "updatedAt": utc_now(),
                }
            self._append_auto_log(
                "info",
                f"{'启用' if enabled else '关闭'} {'全局' if task_root is None else task_root.name} 自动续跑",
            )
            self._save_auto()
            return {
                "globalEnabled": bool(self._auto.get("globalEnabled")),
                "taskEnabled": bool(
                    ((self._auto.get("tasks") or {}).get(str(task_root.resolve()), {}) or {}).get("enabled")
                ) if task_root else None,
            }

    def _append_auto_log(self, level: str, message: str) -> None:
        log = self._auto.setdefault("log", [])
        log.append({"at": utc_now(), "level": level, "message": message})
        del log[:-AUTO_LOG_LIMIT]

    def _task_auto_enabled(self, task_root: Path) -> bool:
        if not self._auto.get("globalEnabled"):
            return False
        record = (self._auto.get("tasks") or {}).get(str(task_root.resolve()), {})
        return bool(record.get("enabled"))

    def _auto_quota_ok(self, task_root: Path, side: str) -> tuple[bool, str]:
        auto_cfg = (self.config.get("monitor") or {}).get("autoResume") or {}
        cooldown = float(auto_cfg.get("cooldownSeconds") or 180)
        window = float(auto_cfg.get("windowSeconds") or 3600)
        limit = int(auto_cfg.get("maxRelaunchesPerSide") or 3)
        key = JobManager.key(task_root, side)
        now = time.time()
        last = float((self._auto.get("lastRunAt") or {}).get(key) or 0)
        if last and now - last < cooldown:
            return False, f"冷却中 {int(cooldown - (now - last))}s"
        history = [
            float(value) for value in ((self._auto.get("history") or {}).get(key) or [])
            if isinstance(value, (int, float)) and now - float(value) <= window
        ]
        if len(history) >= limit:
            return False, f"{int(window / 60)} 分钟内已达到 {limit} 次"
        return True, ""

    def maybe_auto_resume(self) -> list[dict[str, Any]]:
        auto_cfg = (self.config.get("monitor") or {}).get("autoResume") or {}
        if not self._auto.get("globalEnabled"):
            return []
        with self._auto_lock:
            actions: list[dict[str, Any]] = []
            running_jobs = self.jobs.running()
            max_jobs = int(auto_cfg.get("maxConcurrentJobs") or 2)
            if len(running_jobs) >= max_jobs:
                return []
            snapshot = self.snapshot(fetch_submissions=False, force_submissions=False)
            for task in snapshot.get("tasks") or []:
                task_root = Path(task["taskRoot"])
                if not self._task_auto_enabled(task_root):
                    continue
                if task.get("stateStatus") in TERMINAL_TASK_STATUSES:
                    continue
                for side in SIDES:
                    side_data = (task.get("sides") or {}).get(side) or {}
                    if not side_data.get("everStarted") or not side_data.get("canResume") or side_data.get("active"):
                        continue
                    if not side_data.get("needsResume"):
                        continue
                    current_job = self.jobs.get(task_root, side)
                    if current_job and current_job.get("status") == "running":
                        continue
                    quota_ok, quota_reason = self._auto_quota_ok(task_root, side)
                    if not quota_ok:
                        continue
                    try:
                        job = self.jobs.start(
                            task_root,
                            side,
                            force=False,
                            reason=f"auto:{side_data.get('health')}",
                        )
                    except MonitorError as exc:
                        self._append_auto_log("warning", f"{task['name']} / {side} 自动续跑失败：{exc}")
                        continue
                    key = JobManager.key(task_root, side)
                    history = self._auto.setdefault("history", {}).setdefault(key, [])
                    history.append(time.time())
                    now = time.time()
                    self._auto.setdefault("lastRunAt", {})[key] = now
                    self._auto["history"][key] = [
                        value for value in history if isinstance(value, (int, float)) and now - float(value) <= 86400
                    ]
                    message = (
                        f"{task['name']} / {side} 自动续跑，原因={side_data.get('health')}，PID={job.get('pid')}"
                    )
                    self._append_auto_log("info", message)
                    actions.append({"task": task["name"], "side": side, "job": job, "message": message})
                    running_jobs = self.jobs.running()
                    if len(running_jobs) >= max_jobs:
                        self._save_auto()
                        return actions
            self._save_auto()
            return actions

    def snapshot(self, *, fetch_submissions: bool = True, force_submissions: bool = False) -> dict[str, Any]:
        cfg_monitor = self.config.get("monitor") or {}
        auto_cfg = cfg_monitor.get("autoResume") or {}
        stale_seconds = float(auto_cfg.get("staleSeconds") or 420)
        docker = self.docker_cache.get()
        now = time.time()
        roots = [Path(value).expanduser().resolve() for value in self.config.get("roots") or [DEFAULT_ROOT]]
        task_roots = discover_task_roots(roots)
        tasks = [
            self._task_snapshot(root, stale_seconds, now, docker)
            for root in task_roots
        ]
        with self._lock:
            dismissed_ids = set(self._dismissed)
        tasks = [task for task in tasks if str(task.get("id") or "") not in dismissed_ids]
        tasks.sort(key=self._task_sort_key, reverse=True)
        queue_snapshot = self.queue.snapshot()
        task_prompts: dict[str, str] = {}
        workdir = roots[0] if roots else Path(str(self.config.get("roots", [DEFAULT_ROOT])[0]))
        queue_sources = [
            item for item in (queue_snapshot.get("items") or [])
            if item.get("status") == "running" and item.get("source") == "platform"
        ]
        queue_sources.extend(self.queue.triggered_items())
        seen_trigger_items: set[str] = set()
        for item in queue_sources:
            item_id = str(item.get("id") or "")
            if item_id and item_id in seen_trigger_items:
                continue
            if item_id:
                seen_trigger_items.add(item_id)
            task_name = Path(str(item.get("taskRoot") or "")).name
            trigger_prompt = str(item.get("triggerPrompt") or "").strip()
            if not task_name or not trigger_prompt:
                continue
            prompt_sha256 = str(item.get("promptSha256") or "")
            if not prompt_sha256:
                prompt = (
                    "本次监控队列已分配唯一任务名。\n"
                    f"- 监控工作目录：`{workdir}`\n"
                    f"- 唯一任务名：`{task_name}`\n"
                    "- 必须使用该任务名创建独立目录，不得复用已存在目录。\n\n"
                    f"{trigger_prompt}\n"
                )
                prompt_sha256 = queue_prompt_sha256(prompt)
            task_prompts[task_name] = prompt_sha256
        app_sessions = scan_codex_sessions(
            roots,
            task_prompts=task_prompts,
            max_idle_sec=float(cfg_monitor.get("sessionMaxIdleSeconds") or 6 * 3600),
        ) if task_prompts else []
        sessions_by_task: dict[str, list[dict[str, Any]]] = {}
        for session in app_sessions:
            task_name = str(session.get("taskName") or "")
            if task_name:
                sessions_by_task.setdefault(task_name, []).append(session)
        for task in tasks:
            matched_sessions = sessions_by_task.get(str(task.get("name") or ""), [])
            if task.get("stateStatus") in TERMINAL_TASK_STATUSES:
                matched_sessions = []
            task["appSessions"] = matched_sessions
            task["appSession"] = task["appSessions"][0] if task["appSessions"] else None
            task["active"] = bool(
                task["appSessions"]
                or any((item or {}).get("active") for item in (task.get("sides") or {}).values())
                or any((item or {}).get("active") for item in (task.get("candidates") or []))
            )
        submissions = (
            self.submissions.get(force=force_submissions)
            if fetch_submissions
            else {"items": [], "source": "not-requested", "error": ""}
        )
        for submission in submissions.get("items") or []:
            matched = [
                task["id"] for task in tasks
                if _submission_matches_task(submission, task)
            ]
            submission["taskIds"] = matched
        return {
            "generatedAt": utc_now(),
            "roots": [str(item) for item in roots],
            "tasks": tasks,
            "summary": self._summary(tasks),
            "submissions": submissions,
            "autoResume": {
                "globalEnabled": bool(self._auto.get("globalEnabled")),
                "tasks": self._auto.get("tasks") or {},
                "log": list((self._auto.get("log") or [])[-30:]),
            },
            "jobs": self.jobs.running(),
            "automation": queue_snapshot,
            "docker": {"error": docker.get("error", "")},
            "config": {
                "pollSeconds": int(cfg_monitor.get("pollSeconds") or 3),
                "staleSeconds": int(stale_seconds),
            },
        }

    def _task_sort_key(self, task: dict[str, Any]) -> float:
        candidates = [task.get("updatedAt"), task.get("createdAt")]
        for side in SIDES:
            side_data = (task.get("sides") or {}).get(side) or {}
            candidates.extend([side_data.get("lastActivityAt"), side_data.get("startedAt")])
        for item in task.get("candidates") or []:
            candidates.extend([item.get("lastActivityAt"), item.get("startedAt"), item.get("finishedAt")])
        timestamps = [parsed.timestamp() for parsed in (parse_time(value) for value in candidates) if parsed]
        return max(timestamps) if timestamps else 0.0

    def _task_snapshot(
        self,
        task_root: Path,
        stale_seconds: float,
        now: float,
        docker: dict[str, Any],
    ) -> dict[str, Any]:
        state = read_json(task_root / "monitor" / "state.json", {})
        if not isinstance(state, dict):
            state = {}
        platform = read_json(task_root / "monitor" / "platform-selection.json", {})
        selection = platform.get("selection") if isinstance(platform, dict) else {}
        if not isinstance(selection, dict):
            selection = {}
        task = {
            "id": short_hash(str(task_root)),
            "taskRoot": str(task_root),
            "name": str(state.get("taskName") or task_root.name),
            "stateStatus": str(state.get("status") or "unknown"),
            "taskType": str(state.get("taskType") or selection.get("taskType") or ""),
            "difficulty": str(state.get("difficulty") or ""),
            "createdAt": str(state.get("createdAt") or ""),
            "updatedAt": str(state.get("updatedAt") or ""),
            "repoName": str(state.get("repoName") or selection.get("repoName") or ""),
            "repoUrl": str(state.get("repoUrl") or ""),
            "initialSnapshot": str(state.get("initialSnapshot") or ""),
            "promptPath": str(state.get("promptPath") or ""),
            "promptSha256": str(state.get("promptSha256") or ""),
            "promptText": "",
            "projectCode": str(selection.get("projectCode") or ""),
            "projectName": str(selection.get("projectName") or ""),
            "taskNo": str(selection.get("taskNo") or ""),
            "variantName": str(selection.get("variantName") or ""),
            "sides": {},
            "candidates": [],
            "candidateMapping": copy.deepcopy(state.get("candidateMapping") or {}),
            "candidateCount": int(state.get("candidateCount") or 0),
            "artifacts": {
                "semanticA": (task_root / "monitor" / "semantic" / "a.review.json").is_file(),
                "semanticB": (task_root / "monitor" / "semantic" / "b.review.json").is_file(),
                "evidence": (task_root / "monitor" / "evidence.json").is_file(),
                "audit": (task_root / "monitor" / "audit.json").is_file(),
                "gsb": (task_root / "workspace" / "评审文件" / "交付表.xlsx").is_file(),
            },
            "autoResume": self._task_auto_enabled(task_root),
            "localHead": {side: "" for side in SIDES},
        }
        prompt_path = Path(task["promptPath"]) if task["promptPath"] else None
        if prompt_path and prompt_path.is_file():
            try:
                task["promptText"] = prompt_path.read_text(encoding="utf-8", errors="replace")[:20000]
            except OSError:
                task["promptText"] = ""
        for side in SIDES:
            task["sides"][side] = _side_snapshot(
                task_root,
                task["name"],
                state,
                side,
                self.trace_cache,
                docker,
                stale_seconds,
                now,
            )
            workspace = str((state.get("sides") or {}).get(side, {}).get("workspacePath") or "")
            task["localHead"][side] = _git_head(Path(workspace)) if workspace else _git_head(task_root / "source" / side.lower())
        configured_ids = [str(item) for item in (state.get("candidateIds") or []) if _is_candidate_id(item)]
        state_candidates = state.get("candidates") if isinstance(state.get("candidates"), dict) else {}
        all_candidate_ids = list(dict.fromkeys(configured_ids + [str(item) for item in state_candidates if _is_candidate_id(item)]))
        all_candidate_ids.sort(key=lambda item: int(item.split("-")[-1]))
        task["candidates"] = [
            _candidate_snapshot(
                task_root,
                task["name"],
                state,
                candidate,
                self.trace_cache,
                docker,
                stale_seconds,
                now,
            )
            for candidate in all_candidate_ids
        ]
        task["workflow"] = _workflow_steps(task_root, state, task)
        task["needsAttention"] = any(
            item.get("needsResume") or item.get("apiRetries")
            for item in [*task["sides"].values(), *task["candidates"]]
        )
        return task

    @staticmethod
    def _summary(tasks: list[dict[str, Any]]) -> dict[str, Any]:
        active_tasks = 0
        active_instances = 0
        stale_tasks = 0
        stale_instances = 0
        blocked_tasks = 0
        blocked_instances = 0
        staged = 0
        active_session_ids: set[str] = set()
        for task in tasks:
            task_active = False
            task_stale = False
            task_blocked = False
            for side in SIDES:
                data = (task.get("sides") or {}).get(side) or {}
                if data.get("active"):
                    active_instances += 1
                    task_active = True
                if data.get("stale"):
                    stale_instances += 1
                    task_stale = True
                if data.get("status") == "blocked":
                    blocked_instances += 1
                    task_blocked = True
                if data.get("status") in SIDE_DONE_STATUSES:
                    staged += 1
            for data in task.get("candidates") or []:
                if data.get("mappedSide"):
                    continue
                if data.get("active"):
                    active_instances += 1
                    task_active = True
                if data.get("stale"):
                    stale_instances += 1
                    task_stale = True
                if data.get("status") == "blocked":
                    blocked_instances += 1
                    task_blocked = True
            app_sessions = task.get("appSessions") or []
            for session in app_sessions:
                thread_id = str(session.get("threadId") or session.get("sessionId") or "")
                if thread_id:
                    active_session_ids.add(thread_id)
                task_active = True
            if not task_active and task.get("active"):
                task_active = True
            active_tasks += int(task_active)
            stale_tasks += int(task_stale)
            blocked_tasks += int(task_blocked)
        return {
            "tasks": len(tasks),
            "activeTasks": active_tasks,
            "activeInstances": active_instances,
            "activeAppSessions": len(active_session_ids),
            "activeSides": active_instances,
            "staleTasks": stale_tasks,
            "staleInstances": stale_instances,
            "staleSides": stale_instances,
            "blockedTasks": blocked_tasks,
            "blockedInstances": blocked_instances,
            "blockedSides": blocked_instances,
            "stagedSides": staged,
        }

    def task_by_id(self, task_id: str) -> dict[str, Any] | None:
        snapshot = self.snapshot(fetch_submissions=False)
        for task in snapshot.get("tasks") or []:
            if task.get("id") == task_id:
                return task
        return None

    def dismiss_task(self, task_id: str) -> dict[str, Any]:
        task_id = str(task_id or "").strip()
        if not task_id:
            raise MonitorError("缺少任务 ID")
        task = self.task_by_id(task_id)
        if not task:
            raise MonitorError("任务不存在或已放弃监控")
        record = {
            "taskId": task_id,
            "taskRoot": str(task.get("taskRoot") or ""),
            "name": str(task.get("name") or ""),
            "dismissedAt": utc_now(),
        }
        with self._lock:
            self._dismissed[task_id] = record
            self._save_dismissed_tasks_locked()
        return record

    def history(self, task_id: str, side: str, *, event_limit: int = 400) -> dict[str, Any]:
        task = self.task_by_id(task_id)
        if not task:
            raise MonitorError("任务不存在")
        requested = str(side).strip()
        normalized = requested.upper()
        if normalized in SIDES:
            identifier = normalized
        elif _is_candidate_id(requested.lower()):
            identifier = requested.lower()
        else:
            raise MonitorError("side 只能为 A、B 或 candidate-N")
        task_root = Path(task["taskRoot"])
        state = read_json(task_root / "monitor" / "state.json", {})
        if not isinstance(state, dict):
            state = {}
        if normalized in SIDES:
            state_side = (state.get("sides") or {}).get(identifier) or {}
        else:
            state_side = copy.deepcopy((state.get("candidates") or {}).get(identifier) or {})
            state_side["candidateId"] = identifier
        attempts = _attempt_history(
            task_root,
            identifier,
            state_side,
            self.trace_cache,
            self.docker_cache.get(),
            include_events=True,
            event_limit=event_limit,
        )
        return {
            "taskId": task_id,
            "taskRoot": str(task_root),
            "side": identifier,
            "attempts": attempts,
            "generatedAt": utc_now(),
        }

    def automation_action(self, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        action = str(action or "").strip()
        if action == "set-roots":
            roots = payload.get("roots")
            if not isinstance(roots, list):
                raise MonitorError("roots 必须是数组")
            return self.queue.set_roots([str(item) for item in roots])
        if action == "set-capacity":
            return self.queue.set_capacity(int(payload.get("capacity") or 0))
        if action == "set-cooldown":
            return self.queue.set_cooldown(int(payload.get("cooldownSeconds") or 0))
        if action == "set-paused":
            return self.queue.set_paused(bool(payload.get("paused")))
        if action == "queue-add":
            task = self.task_by_id(str(payload.get("taskId") or ""))
            if not task:
                raise MonitorError("任务不存在")
            side = str(payload.get("side") or "both")
            self.queue.add(Path(task["taskRoot"]), side)
            return self.queue.snapshot()
        if action == "queue-add-platform":
            project = payload.get("project")
            if not isinstance(project, dict):
                raise MonitorError("缺少平台项目数据")
            task_type = str(payload.get("taskType") or "0-1代码生成")
            difficulty = str(payload.get("difficulty") or "困难")
            template = str(self.config.get("automation", {}).get("promptTemplate") or DEFAULT_AUTO_TRIGGER_PROMPT)
            self.queue.add_platform(
                project,
                task_type=task_type,
                difficulty=difficulty,
                side=str(payload.get("side") or "both"),
                trigger_prompt=render_auto_trigger_prompt(
                    template,
                    project,
                    task_type=task_type,
                    difficulty=difficulty,
                ),
            )
            return self.queue.snapshot()
        if action == "queue-remove":
            self.queue.remove(str(payload.get("itemId") or ""))
            return self.queue.snapshot()
        if action == "queue-move":
            self.queue.move(str(payload.get("itemId") or ""), int(payload.get("delta") or 0))
            return self.queue.snapshot()
        if action == "queue-retry":
            self.queue.retry(str(payload.get("itemId") or ""))
            return self.queue.snapshot()
        if action == "queue-clear":
            self.queue.clear_finished()
            return self.queue.snapshot()
        raise MonitorError(f"未知 automation action: {action}")

    def platform_candidates(self, task_type: str = "0-1代码生成", force: bool = False) -> dict[str, Any]:
        return self.platform.candidates(task_type=task_type, force=force)

    def start_action(self, task_id: str, side: str, mode: str = "resume") -> dict[str, Any]:
        task = self.task_by_id(task_id)
        if not task:
            raise MonitorError("任务不存在或已移出扫描目录")
        side = str(side).upper()
        if mode == "both":
            side = "BOTH"
        if side not in {"A", "B", "BOTH"}:
            raise MonitorError("side 只能为 A、B 或 both")
        if mode not in {"resume", "rerun", "both"}:
            raise MonitorError("mode 只能为 resume、rerun 或 both")
        force = mode == "rerun"
        if side == "BOTH":
            active_sides = [
                key for key in SIDES
                if ((task.get("sides") or {}).get(key) or {}).get("active")
            ]
            if active_sides:
                raise MonitorError(f"{'/'.join(active_sides)} 正在运行，不能并行续跑")
        else:
            current = (task.get("sides") or {}).get(side) or {}
            if current.get("active") and not force:
                raise MonitorError(f"{side} 正在运行，不能重复启动")
        return self.jobs.start(Path(task["taskRoot"]), side, force=force, reason=mode)

    def tail_log(self, task_id: str, side: str, kind: str = "events", lines: int = 200) -> dict[str, Any]:
        task = self.task_by_id(task_id)
        if not task:
            raise MonitorError("任务不存在")
        side = str(side).upper()
        if side not in SIDES:
            raise MonitorError("side 只能为 A 或 B")
        if kind == "job":
            return self.jobs.tail_log(Path(task["taskRoot"]), side, lines)
        side_data = (task.get("sides") or {}).get(side) or {}
        path_value = str(side_data.get("stdoutPath") or "")
        path = Path(path_value) if path_value else None
        if path is None or not path.is_file():
            return {"lines": [], "path": path_value}
        try:
            raw_lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            raw_lines = []
        result: list[str] = []
        for raw in raw_lines[-max(1, min(lines, 2000)):]:
            try:
                event = json.loads(raw)
            except ValueError:
                result.append(redact_text(raw, 1500))
                continue
            event_type = str(event.get("type") or "")
            if event_type == "assistant":
                blocks = _content_blocks(event)
                details = []
                for block in blocks:
                    block_type = str(block.get("type") or "")
                    if block_type == "text":
                        details.append(redact_text(block.get("text") or "", 700))
                    elif block_type == "tool_use":
                        details.append(f"tool={block.get('name')} {_tool_detail(block)}")
                    elif block_type == "thinking":
                        details.append("thinking")
                result.append("assistant | " + " | ".join(item for item in details if item))
            elif event_type == "user":
                blocks = _content_blocks(event)
                texts = []
                for block in blocks:
                    if block.get("type") == "tool_result":
                        texts.append(redact_text(block.get("content") or "", 700))
                if texts:
                    result.append("tool_result | " + " | ".join(texts))
            else:
                result.append(redact_text(raw, 1200))
        return {"lines": result, "path": str(path)}
