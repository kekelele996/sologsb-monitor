#!/usr/bin/env python3
"""Read active Codex App sessions and filter them by workspace root."""
from __future__ import annotations

import json
import hashlib
import os
import re
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

CODEX_HOME = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
SESSIONS_DIR = CODEX_HOME / "sessions"
THREAD_HISTORY_DB = CODEX_HOME / "thread_history_1.sqlite"
THREAD_ID_RE = re.compile(
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
    re.I,
)
ACTIVE_TURN_STATUSES = {"inprogress", "running"}


def queue_prompt_sha256(text: str) -> str:
    normalized = "\n".join(line.strip() for line in str(text or "").splitlines() if line.strip())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _day_dirs(days: int) -> list[Path]:
    now = datetime.now()
    return [
        SESSIONS_DIR / f"{(now - timedelta(days=offset)).year:04d}" / f"{(now - timedelta(days=offset)).month:02d}" / f"{(now - timedelta(days=offset)).day:02d}"
        for offset in range(max(1, days))
    ]


def _thread_id_from_path(path: Path) -> str:
    match = THREAD_ID_RE.search(path.stem)
    return match.group(1) if match else ""


def _session_meta(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(100):
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") not in {"session_meta", "turn_context"}:
                    continue
                payload = event.get("payload")
                if isinstance(payload, dict):
                    return payload
    except OSError:
        pass
    return {}


def _fallback_turn_status(path: Path) -> str:
    started = complete = 0
    last_event = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if '"task_started"' not in line and '"task_complete"' not in line and '"turn_aborted"' not in line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                payload = event.get("payload") if isinstance(event, dict) else {}
                event_type = str(payload.get("type") or "") if isinstance(payload, dict) else ""
                if event_type == "task_started":
                    started += 1
                    last_event = event_type
                elif event_type == "task_complete":
                    complete += 1
                    last_event = event_type
                elif event_type == "turn_aborted":
                    last_event = event_type
    except OSError:
        return "unknown"
    if last_event == "task_complete" or complete >= started:
        return "completed"
    if last_event == "turn_aborted":
        return "interrupted"
    if started > complete:
        return "inProgress"
    return "unknown"


def _thread_turn_statuses() -> dict[str, dict[str, Any]]:
    if not THREAD_HISTORY_DB.is_file():
        return {}
    statuses: dict[str, dict[str, Any]] = {}
    try:
        connection = sqlite3.connect(f"file:{THREAD_HISTORY_DB}?mode=ro", uri=True)
        rows = connection.execute(
            "SELECT thread_id, turn_id, status, rollout_ordinal, started_at, completed_at, error_json "
            "FROM thread_turns"
        )
        for thread_id, turn_id, status, ordinal, started_at, completed_at, error in rows:
            previous = statuses.get(str(thread_id))
            if previous and int(previous.get("rolloutOrdinal") or -1) >= int(ordinal or -1):
                continue
            statuses[str(thread_id)] = {
                "turnId": turn_id,
                "status": status,
                "rolloutOrdinal": ordinal,
                "startedAt": started_at,
                "completedAt": completed_at,
                "error": error,
            }
        connection.close()
    except (OSError, sqlite3.Error):
        return {}
    return statuses


def _within_roots(cwd: str, roots: list[Path]) -> bool:
    if not roots:
        return True
    try:
        path = Path(cwd).expanduser().resolve()
    except (OSError, ValueError):
        return False
    return any(path == root or root in path.parents for root in roots)


def _matches_task_names(path: Path, task_names: list[str]) -> str:
    if not task_names:
        return ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            head = handle.read(512 * 1024)
    except OSError:
        return ""
    for name in task_names:
        if name and name in head:
            return name
    return ""


def _queue_user_text(path: Path) -> str:
    marker = "本次监控队列已分配唯一任务名"
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for _ in range(200):
                line = handle.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(event, dict) or event.get("type") != "response_item":
                    continue
                payload = event.get("payload")
                if not isinstance(payload, dict) or payload.get("type") != "message" or payload.get("role") != "user":
                    continue
                content = payload.get("content")
                if not isinstance(content, list):
                    continue
                text = "".join(
                    str(item.get("text") or "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") in {"input_text", "text"}
                )
                if marker in text:
                    return text
    except OSError:
        pass
    return ""


def _matches_task_prompts(path: Path, task_prompts: dict[str, str]) -> str:
    if not task_prompts:
        return ""
    text = _queue_user_text(path)
    if not text:
        return ""
    digest = queue_prompt_sha256(text)
    for task_name, expected in task_prompts.items():
        if digest == expected:
            return task_name
    return ""


def active_sessions(
    allowed_roots: Iterable[str | Path] | None = None,
    *,
    task_names: Iterable[str] | None = None,
    task_prompts: dict[str, str] | None = None,
    max_idle_sec: float = 6 * 3600,
    days: int = 2,
) -> list[dict[str, Any]]:
    """Return active Codex App sessions under the configured roots.

    The latest `thread_turns.status` from the app's SQLite database is
    authoritative. JSONL lifecycle events are only a fallback.
    """
    roots = []
    for value in allowed_roots or []:
        try:
            roots.append(Path(value).expanduser().resolve())
        except (OSError, ValueError):
            continue
    names = [str(value) for value in (task_names or []) if str(value)]
    prompt_map = {str(key): str(value) for key, value in (task_prompts or {}).items() if value}
    now = time.time()
    statuses = _thread_turn_statuses()
    sessions: list[dict[str, Any]] = []
    seen: set[str] = set()
    for day in _day_dirs(days):
        if not day.is_dir():
            continue
        for path in day.glob("rollout-*.jsonl"):
            thread_id = _thread_id_from_path(path)
            if not thread_id or thread_id in seen:
                continue
            try:
                stat = path.stat()
            except OSError:
                continue
            age = max(0.0, now - stat.st_mtime)
            if age > max_idle_sec:
                continue
            meta = _session_meta(path)
            cwd = str(meta.get("cwd") or "")
            if not _within_roots(cwd, roots):
                continue
            turn = statuses.get(thread_id) or {}
            status = str(turn.get("status") or _fallback_turn_status(path))
            if status.strip().casefold() not in ACTIVE_TURN_STATUSES:
                continue
            task_name = _matches_task_prompts(path, prompt_map) if prompt_map else _matches_task_names(path, names)
            if (prompt_map or names) and not task_name:
                continue
            seen.add(thread_id)
            sessions.append({
                "threadId": thread_id,
                "thread": thread_id[-12:],
                "sessionId": thread_id,
                "cwd": cwd,
                "status": status,
                "active": True,
                "taskName": task_name,
                "startedAt": turn.get("startedAt") or "",
                "completedAt": turn.get("completedAt") or "",
                "turnId": turn.get("turnId") or "",
                "turnUpdatedAt": turn.get("completedAt") or turn.get("startedAt") or "",
                "lastActivityAt": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(),
                "ageSeconds": age,
                "rolloutPath": str(path),
            })
    sessions.sort(key=lambda item: float(item.get("ageSeconds") or 0))
    return sessions
