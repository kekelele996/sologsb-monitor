import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import queue_worker  # noqa: E402


class QueueWorkerTests(unittest.TestCase):
    def _fake_helper(self, root: Path) -> Path:
        executable = root / "CodexQueuePush"
        executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        executable.chmod(0o755)
        return executable

    def test_platform_task_opens_chatgpt_with_workspace_and_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = self._fake_helper(root)
            workdir = root / "work"
            workdir.mkdir()
            prompt_file = root / "trigger.prompt.md"
            prompt_file.write_text("使用 `$sologsb-0917` 完整执行任务。", encoding="utf-8")
            result_file = root / "result.json"
            argv = [
                "queue_worker.py",
                "--push-helper", str(helper),
                "--workdir", str(workdir),
                "--task-name", "cy-999-20260918-120000",
                "--project-code", "cy-999",
                "--wait-timeout", "0",
                "--result-file", str(result_file),
                "--log-file", str(result_file.with_suffix(".log")),
                "--trigger-prompt-file", str(prompt_file),
            ]
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                queue_worker.subprocess, "run", return_value=completed
            ) as run:
                self.assertEqual(queue_worker.main(), 0)

            command = run.call_args.args[0]
            self.assertEqual(command[0], str(helper.resolve()))
            self.assertTrue(command[1].startswith("codex://threads/new?"))
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(command[1]).query)
            self.assertEqual(query["path"], [str(workdir.resolve())])
            self.assertEqual(query["mode"], ["work"])
            self.assertIn("cy-999-20260918-120000", query["prompt"][0])
            self.assertIn("$sologsb-0917", query["prompt"][0])
            metadata = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "finished")
            self.assertEqual(metadata["stage"], "desktop-submitted")
            run_log = result_file.with_suffix(".log").read_text(encoding="utf-8")
            self.assertIn("正在打开 ChatGPT", run_log)
            self.assertIn("任务已提交", run_log)
            self.assertEqual(Path(metadata["taskRoot"]), (workdir / "cy-999-20260918-120000").resolve())

    def test_completed_task_state_marks_worker_finished(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = self._fake_helper(root)
            workdir = root / "work"
            task_root = workdir / "cy-999-20260918-120000"
            (task_root / "monitor").mkdir(parents=True)
            (task_root / "monitor" / "state.json").write_text(
                json.dumps({"status": "complete"}), encoding="utf-8"
            )
            prompt_file = root / "trigger.prompt.md"
            prompt_file.write_text("执行任务。", encoding="utf-8")
            result_file = root / "result.json"
            argv = [
                "queue_worker.py",
                "--push-helper", str(helper),
                "--workdir", str(workdir),
                "--task-name", "cy-999-20260918-120000",
                "--project-code", "cy-999",
                "--wait-timeout", "2",
                "--result-file", str(result_file),
                "--log-file", str(result_file.with_suffix(".log")),
                "--trigger-prompt-file", str(prompt_file),
            ]
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                queue_worker.subprocess, "run", return_value=completed
            ):
                self.assertEqual(queue_worker.main(), 0)
            metadata = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "finished")
            self.assertEqual(metadata["stage"], "task-complete")

    def test_missing_trigger_prompt_fails_before_starting_helper(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = self._fake_helper(root)
            workdir = root / "work"
            workdir.mkdir()
            result_file = root / "result.json"
            argv = [
                "queue_worker.py",
                "--push-helper", str(helper),
                "--workdir", str(workdir),
                "--task-name", "cy-999-20260918-120000",
                "--project-code", "cy-999",
                "--result-file", str(result_file),
                "--log-file", str(result_file.with_suffix(".log")),
                "--trigger-prompt-file", str(root / "missing.prompt.md"),
            ]
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                queue_worker.subprocess, "run"
            ) as run:
                self.assertEqual(queue_worker.main(), 2)
            run.assert_not_called()
            metadata = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(metadata["stage"], "validate-prompt")

    def test_accessibility_permission_error_is_reported(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = self._fake_helper(root)
            workdir = root / "work"
            workdir.mkdir()
            prompt_file = root / "prompt.md"
            prompt_file.write_text("执行任务。", encoding="utf-8")
            result_file = root / "result.json"
            argv = [
                "queue_worker.py",
                "--push-helper", str(helper),
                "--workdir", str(workdir),
                "--task-name", "cy-999-20260918-120000",
                "--project-code", "cy-999",
                "--result-file", str(result_file),
                "--log-file", str(result_file.with_suffix(".log")),
                "--trigger-prompt-file", str(prompt_file),
            ]
            completed = subprocess.CompletedProcess(args=[], returncode=3, stdout="", stderr="")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                queue_worker.subprocess, "run", return_value=completed
            ):
                self.assertEqual(queue_worker.main(), 2)
            metadata = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "failed")
            self.assertIn("辅助功能权限", metadata["error"])

    def test_init_failure_marks_worker_failed_without_waiting_for_timeout(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            helper = self._fake_helper(root)
            workdir = root / "work"
            task_root = workdir / "cy-999-20260918-120000"
            (task_root / "monitor").mkdir(parents=True)
            (task_root / "monitor" / "init-failure.json").write_text(
                json.dumps({"status": "blocked", "error": "项目已被占用"}), encoding="utf-8"
            )
            prompt_file = root / "prompt.md"
            prompt_file.write_text("执行任务。", encoding="utf-8")
            result_file = root / "result.json"
            argv = [
                "queue_worker.py",
                "--push-helper", str(helper),
                "--workdir", str(workdir),
                "--task-name", "cy-999-20260918-120000",
                "--project-code", "cy-999",
                "--wait-timeout", "30",
                "--result-file", str(result_file),
                "--log-file", str(result_file.with_suffix(".log")),
                "--trigger-prompt-file", str(prompt_file),
            ]
            completed = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
            with mock.patch.object(sys, "argv", argv), mock.patch.object(
                queue_worker.subprocess, "run", return_value=completed
            ):
                self.assertEqual(queue_worker.main(), 2)
            metadata = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(metadata["stage"], "task-init")
            self.assertEqual(metadata["error"], "项目已被占用")

    def test_missing_task_root_times_out_before_full_wait(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            workdir = root / "work"
            workdir.mkdir()
            task_root = workdir / "cy-999-20260918-120000"
            trigger_prompt = root / "prompt.md"
            trigger_prompt.write_text("执行任务。", encoding="utf-8")
            result_file = root / "result.json"
            writer = queue_worker.LogWriter(root / "worker.log")
            with mock.patch.object(queue_worker.time, "monotonic", return_value=10.0):
                code = queue_worker.wait_for_task(
                    result_file,
                    workdir=workdir,
                    task_root=task_root,
                    args=mock.Mock(),
                    push_helper=root / "CodexQueuePush",
                    trigger_prompt_file=trigger_prompt,
                    deep_link="codex://threads/new",
                    timeout=100,
                    startup_timeout=5,
                    submitted_at=0.0,
                    writer=writer,
                )
            self.assertEqual(code, 2)
            metadata = json.loads(result_file.read_text(encoding="utf-8"))
            self.assertEqual(metadata["status"], "failed")
            self.assertEqual(metadata["stage"], "desktop-start-timeout")
            self.assertIn("未创建任务目录", metadata["error"])


if __name__ == "__main__":
    unittest.main()
