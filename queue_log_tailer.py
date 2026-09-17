#!/usr/bin/env python3
"""Follow a ChatGPT rollout and expose it as a queue log."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from queue_log import LogWriter, RolloutLogFollower, discover_rollout, find_rollout_by_thread, public_log_path


def read_status(path: Path) -> str:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    return str(value.get("status") or "") if isinstance(value, dict) else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log-id", required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--thread-id", default="")
    parser.add_argument("--state-file", type=Path)
    parser.add_argument("--timeout", type=float, default=12 * 60 * 60)
    args = parser.parse_args()

    writer = LogWriter(public_log_path(args.log_id))
    writer.emit(f"[日志] 开始跟随队列任务 {args.task_name}")
    rollout = find_rollout_by_thread(args.thread_id) if args.thread_id else None
    if rollout is None:
        rollout = discover_rollout(args.task_name, timeout=30)
    if rollout is None:
        writer.emit("[失败] 未找到 ChatGPT rollout")
        return 2
    writer.emit(f"[日志] ChatGPT 轨迹：{rollout}")
    follower = RolloutLogFollower(rollout, writer, args.task_name)
    follower.pump()

    deadline = time.monotonic() + args.timeout
    idle_after_complete = 0
    while time.monotonic() < deadline:
        written = follower.pump()
        status = read_status(args.state_file) if args.state_file else ""
        if status == "complete" and written == 0:
            idle_after_complete += 1
            if idle_after_complete >= 2:
                writer.emit("[完成] 轨迹跟随结束")
                return 0
        else:
            idle_after_complete = 0
        time.sleep(1)
    writer.emit("[失败] 轨迹跟随超时")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
