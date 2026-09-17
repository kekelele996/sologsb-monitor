import json
import sys
import tempfile
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

from monitor_core import (  # noqa: E402
    JobManager,
    MonitorService,
    QueueManager,
    TraceCache,
    _submission_matches_task,
    discover_task_roots,
    load_config,
    render_auto_trigger_prompt,
)


class MonitorCoreTests(unittest.TestCase):
    def test_discover_task_root(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "case-a"
            (task / "monitor").mkdir(parents=True)
            (task / "monitor" / "state.json").write_text(
                json.dumps(
                    {
                        "taskName": "case-a",
                        "status": "running",
                        "taskRoot": str(task),
                        "sides": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            self.assertEqual(discover_task_roots([root]), [task.resolve()])

    def test_trace_cache_reads_stream_json(self):
        with tempfile.TemporaryDirectory() as temp:
            trace = Path(temp) / "stdout.jsonl"
            events = [
                {
                    "type": "system",
                    "subtype": "init",
                    "session_id": "11111111-1111-1111-1111-111111111111",
                    "model": "public/model",
                    "claude_code_version": "2.1.197",
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "thinking"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}},
                        ]
                    },
                },
                {
                    "type": "user",
                    "message": {
                        "content": [
                            {"type": "tool_result", "content": "README.md"},
                        ]
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "content": [
                            {
                                "type": "tool_use",
                                "name": "TodoWrite",
                                "input": {
                                    "todos": [
                                        {"content": "实现接口", "status": "in_progress", "activeForm": "正在实现接口"},
                                        {"content": "补充测试", "status": "pending"},
                                    ]
                                },
                            }
                        ]
                    },
                },
                {
                    "type": "result",
                    "subtype": "success",
                    "stop_reason": "end_turn",
                    "num_turns": 2,
                },
            ]
            trace.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in events) + "\n", encoding="utf-8")
            stats = TraceCache().stats(trace)
            self.assertEqual(stats["sessionId"], "11111111-1111-1111-1111-111111111111")
            self.assertEqual(stats["toolCalls"], 2)
            self.assertEqual(stats["toolResults"], 1)
            self.assertEqual(stats["lastPhase"], "done")
            self.assertEqual(stats["result"]["stop_reason"], "end_turn")
            self.assertEqual(len(stats["todos"]), 2)
            self.assertEqual(stats["todos"][0]["status"], "in_progress")

    def test_auto_trigger_prompt_renders_selected_project(self):
        template = "执行 {{selected_project}} / {{project_code}} / {{task_type}} / {{difficulty}}"
        rendered = render_auto_trigger_prompt(
            template,
            {"code": "PROJECT-CODE", "name": "测试项目"},
            task_type="0-1代码生成",
            difficulty="困难",
        )
        self.assertEqual(rendered, "执行 PROJECT-CODE · 测试项目 / PROJECT-CODE / 0-1代码生成 / 困难")

    def test_platform_queue_has_no_manual_ab_side_selector(self):
        html = (APP / "static" / "tasks.html").read_text(encoding="utf-8")
        self.assertNotIn('id="platformSide"', html)
        self.assertIn('item.source === "platform" ? "候选竞速"', html)

    def test_platform_queue_item_snapshots_rendered_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_config(roots=[str(root)])
            manager = QueueManager(config, JobManager(config), state_path=root / "queue.json")
            item = manager.add_platform(
                {"code": "PROJECT-CODE", "name": "平台项目"},
                task_type="0-1代码生成",
                difficulty="困难",
                trigger_prompt="执行平台项目 PROJECT-CODE",
            )
            self.assertEqual(item["triggerPrompt"], "执行平台项目 PROJECT-CODE")
            self.assertIn("promptTemplate", manager.snapshot())

    def test_queue_add_remove(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "case-queue"
            (task / "monitor").mkdir(parents=True)
            (task / "monitor" / "state.json").write_text(
                json.dumps({"taskName": "case-queue", "status": "repo_ready", "sides": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            config = load_config(roots=[str(root)])
            manager = QueueManager(config, JobManager(config), state_path=root / "queue.json")
            item = manager.add(task, "both")
            self.assertEqual(manager.snapshot()["counts"]["pending"], 1)
            manager.remove(item["id"])
            self.assertEqual(manager.snapshot()["counts"]["pending"], 0)

    def test_submission_match(self):
        task = {"repoName": "cy402-hearing-schedule", "projectCode": "PROJECT-CODE", "sides": {}}
        self.assertTrue(_submission_matches_task({"repo": "owner/cy402-hearing-schedule"}, task))
        self.assertTrue(_submission_matches_task({"prompt": "实现 PROJECT-CODE 系统"}, task))
        self.assertFalse(_submission_matches_task({"repo": "owner/other"}, task))

    def test_snapshot_exposes_candidate_race_before_ab_mapping(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "case-candidates"
            monitor = task / "monitor"
            runtime = monitor / "runtime" / "candidates" / "candidate-1" / "attempt-01"
            runtime.mkdir(parents=True)
            (task / "source" / "candidates" / "candidate-1").mkdir(parents=True)
            (monitor / "state.json").write_text(
                json.dumps(
                    {
                        "taskName": "case-candidates",
                        "status": "candidates_running",
                        "candidateCount": 3,
                        "candidateIds": ["candidate-1", "candidate-2", "candidate-3"],
                        "candidates": {
                            "candidate-1": {
                                "candidateId": "candidate-1",
                                "status": "running",
                                "attempt": 1,
                                "runPid": 0,
                            }
                        },
                        "sides": {},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (runtime / "attempt.json").write_text(
                json.dumps(
                    {
                        "candidateId": "candidate-1",
                        "attempt": 1,
                        "sessionId": "33333333-3333-3333-3333-333333333333",
                        "container": {"name": "sologsb-case-candidates-candidate-1-1-abc"},
                        "startedAt": "2026-09-17T06:00:00Z",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (runtime / "stdout.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "candidate working"}]},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            service = MonitorService(load_config(roots=[str(root)]))
            snapshot = service.snapshot(fetch_submissions=False)
            candidates = snapshot["tasks"][0]["candidates"]
            self.assertEqual([item["candidateId"] for item in candidates], ["candidate-1", "candidate-2", "candidate-3"])
            self.assertEqual(candidates[0]["trace"]["lastText"], "candidate working")
            self.assertTrue(candidates[0]["events"])
            self.assertTrue(any(step["key"] == "candidates" for step in snapshot["tasks"][0]["workflow"]["steps"]))
            history = service.history(snapshot["tasks"][0]["id"], "candidate-1")
            self.assertEqual(history["side"], "candidate-1")
            self.assertTrue(history["attempts"])

    def test_frontend_uses_dynamic_candidate_dots_and_logs(self):
        html = (APP / "static" / "index.html").read_text(encoding="utf-8")
        self.assertIn("mini-dot", html)
        self.assertIn("renderCandidateLogHtml", html)
        self.assertIn(".mini-dot.running { border-color:var(--warn)", html)
        self.assertIn(".mini-dot.done { border-color:var(--ok)", html)
        self.assertIn(".mini-dot.failed, .mini-dot.stale { border-color:var(--bad)", html)

    def test_snapshot_with_fake_docker(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "case-b"
            monitor = task / "monitor"
            runtime = monitor / "runtime" / "a" / "attempt-01"
            runtime.mkdir(parents=True)
            (monitor / "state.json").write_text(
                json.dumps(
                    {
                        "taskName": "case-b",
                        "status": "running",
                        "taskType": "0-1代码生成",
                        "difficulty": "困难",
                        "sides": {"A": {"status": "running", "attempt": 1, "runPid": 0}},
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (runtime / "attempt.json").write_text(
                json.dumps(
                    {
                        "side": "A",
                        "attempt": 1,
                        "sessionId": "22222222-2222-2222-2222-222222222222",
                        "container": {"name": "sologsb-case-b-a-1-abc"},
                        "startedAt": "2026-09-17T06:00:00Z",
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            (runtime / "stdout.jsonl").write_text(
                json.dumps(
                    {
                        "type": "assistant",
                        "message": {"content": [{"type": "text", "text": "working"}]},
                    },
                    ensure_ascii=False,
                )
                + "\n",
                encoding="utf-8",
            )
            config = load_config(roots=[str(root)])
            service = MonitorService(config)
            snapshot = service.snapshot(fetch_submissions=False)
            self.assertEqual(len(snapshot["tasks"]), 1)
            side = snapshot["tasks"][0]["sides"]["A"]
            self.assertEqual(side["attempt"], 1)
            self.assertEqual(side["trace"]["lastText"], "working")
            self.assertTrue(side["canResume"])
            self.assertGreaterEqual(len(snapshot["tasks"][0]["workflow"]["steps"]), 5)


if __name__ == "__main__":
    unittest.main()
