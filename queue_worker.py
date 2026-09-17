#!/usr/bin/env python3
"""Create a Solo Manager task and run its A/B sides for the automation queue."""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def parse_last_json(text: str) -> dict:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("init 输出中没有 JSON 对象")


def write_result(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skill-script", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--task-name", required=True)
    parser.add_argument("--project-code", required=True)
    parser.add_argument("--task-type", default="0-1代码生成")
    parser.add_argument("--difficulty", default="困难")
    parser.add_argument("--side", default="both")
    parser.add_argument("--result-file", type=Path, required=True)
    parser.add_argument("--trigger-prompt-file", type=Path)
    args = parser.parse_args()

    init_cmd = [
        sys.executable,
        str(args.skill_script),
        "init",
        "--workdir", str(args.workdir),
        "--task-name", args.task_name,
        "--from-platform",
        "--project-code", args.project_code,
        "--task-type", args.task_type,
        "--difficulty", args.difficulty,
    ]
    init = subprocess.run(init_cmd, capture_output=True, text=True, check=False)
    if init.stdout:
        print(init.stdout, flush=True)
    if init.stderr:
        print(init.stderr, file=sys.stderr, flush=True)
    if init.returncode != 0:
        write_result(args.result_file, {
            "status": "failed",
            "stage": "init",
            "exitCode": init.returncode,
            "error": (init.stderr or init.stdout).strip()[-4000:],
        })
        return init.returncode
    try:
        payload = parse_last_json(init.stdout)
    except ValueError as exc:
        write_result(args.result_file, {"status": "failed", "stage": "parse-init", "error": str(exc)})
        print(f"无法解析 init 输出: {exc}", file=sys.stderr)
        return 2
    task_root = Path(str(payload.get("taskRoot") or "")).expanduser().resolve()
    trigger_prompt_path = ""
    if args.trigger_prompt_file and args.trigger_prompt_file.is_file():
        target = task_root / "monitor" / "auto-trigger-prompt.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(args.trigger_prompt_file.read_text(encoding="utf-8"), encoding="utf-8")
        trigger_prompt_path = str(target)
    if not (task_root / "monitor" / "state.json").is_file():
        write_result(args.result_file, {
            "status": "failed",
            "stage": "validate-init",
            "taskRoot": str(task_root),
            "error": "init 返回的 taskRoot 无效",
        })
        return 3
    write_result(args.result_file, {
        "status": "initialized",
        "stage": "run",
        "taskRoot": str(task_root),
        "projectCode": args.project_code,
        "triggerPromptPath": trigger_prompt_path,
    })
    run_cmd = [
        sys.executable,
        str(args.skill_script),
        "run",
        "--task-root", str(task_root),
        "--side", args.side,
    ]
    run = subprocess.run(run_cmd, text=True, check=False)
    write_result(args.result_file, {
        "status": "finished" if run.returncode == 0 else "failed",
        "stage": "run",
        "taskRoot": str(task_root),
        "projectCode": args.project_code,
        "triggerPromptPath": trigger_prompt_path,
        "exitCode": run.returncode,
    })
    return run.returncode


if __name__ == "__main__":
    raise SystemExit(main())
