import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from unittest import mock
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import monitor_core  # noqa: E402
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
                    "model": "auto_model/urm",
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
            {"code": "cy-901", "name": "测试项目"},
            task_type="0-1代码生成",
            difficulty="困难",
        )
        self.assertEqual(rendered, "执行 cy-901 · 测试项目 / cy-901 / 0-1代码生成 / 困难")

    def test_platform_queue_has_no_manual_ab_side_selector(self):
        html = (APP / "static" / "tasks.html").read_text(encoding="utf-8")
        self.assertNotIn('id="platformSide"', html)
        self.assertIn('item.source === "platform" ? "候选竞速"', html)
        self.assertIn('id="inlineLogPanel"', html)
        self.assertIn('id="inlineLogSelect"', html)
        self.assertIn('data-log-id', html)
        self.assertIn('id="platformAvailableCount"', html)
        self.assertIn('id="cooldownInput"', html)
        self.assertIn('api("set-cooldown",{cooldownSeconds:Number($("#cooldownInput").value)})', html)
        self.assertIn('id="saveTemplateBtn"', html)
        self.assertNotIn('id="promptTemplatePreview" readonly', html)
        self.assertIn('api("set-prompt-template", { template })', html)
        self.assertIn('state.promptDirty = true;', html)
        self.assertIn('data-root-active=', html)
        self.assertIn('api("set-root-active",{root:toggle.dataset.rootActive,active:toggle.checked})', html)
        self.assertIn('只有开启“监控中”的目录会被监控页扫描', html)
        self.assertIn("execution.activeTasks", html)

    def test_platform_project_scope_toggle_and_source_badges(self):
        html = (APP / "static" / "tasks.html").read_text(encoding="utf-8")
        self.assertIn('id="mergeProjectPoolToggle"', html)
        self.assertIn('localStorage.getItem("sologsb.mergeProjectPool")', html)
        self.assertIn('(state.platformItems || []).filter(isMineProject)', html)
        self.assertIn('isMineProject(item) ? "我的项目" : "项目池"', html)
        self.assertIn('class="project-source ${projectSourceClass(item)}"', html)
        self.assertIn('function queuedProjectCodes()', html)
        self.assertIn('return items.filter((item) => !queuedCodes.has(String(item.code || "").trim().toLowerCase()));', html)
        self.assertIn('renderPlatform();\n      render();', html)
        self.assertIn('      renderPlatform();\n    }\n    document.addEventListener("change"', html)

    def test_platform_queue_item_snapshots_rendered_prompt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_config(roots=[str(root)])
            manager = QueueManager(config, JobManager(config), state_path=root / "queue.json")
            item = manager.add_platform(
                {"code": "cy-902", "name": "平台项目"},
                task_type="0-1代码生成",
                difficulty="困难",
                trigger_prompt="执行平台项目 cy-902",
            )
            self.assertEqual(item["triggerPrompt"], "执行平台项目 cy-902")
            self.assertIn("promptTemplate", manager.snapshot())

    def test_prompt_template_update_syncs_queued_prompts(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config_path = root / "config.json"
            config = load_config(path=config_path, roots=[str(root)])
            manager = QueueManager(config, JobManager(config), state_path=root / "queue.json")
            item = manager.add_platform(
                {"code": "cy-903", "name": "平台项目"},
                task_type="feature迭代",
                difficulty="地狱",
                trigger_prompt="旧模板 cy-903",
            )
            template = "自定义模板 {{project_code}} / {{task_type}} / {{difficulty}}"
            snapshot = manager.set_prompt_template(template)
            self.assertEqual(snapshot["promptTemplate"], template)
            queued = next(value for value in snapshot["items"] if value["id"] == item["id"])
            self.assertEqual(queued["triggerPrompt"], "自定义模板 cy-903 / feature迭代 / 地狱")
            reloaded = load_config(path=config_path, roots=[str(root)])
            self.assertEqual(reloaded["automation"]["promptTemplate"], template)

    def test_root_activation_controls_monitor_scanning(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root1 = base / "root-one"
            root2 = base / "root-two"
            for root, name in ((root1, "task-one"), (root2, "task-two")):
                monitor = root / name / "monitor"
                monitor.mkdir(parents=True)
                (monitor / "state.json").write_text(
                    json.dumps({"taskName": name, "status": "blocked", "sides": {}}, ensure_ascii=False),
                    encoding="utf-8",
                )
            config_path = base / "config.json"
            config = load_config(path=config_path, roots=[str(root1), str(root2)])
            config["monitor"]["activeRoots"] = [str(root1)]
            with mock.patch.object(monitor_core, "STATE_DIR", base / "state"):
                service = MonitorService(config)
                snapshot = service.snapshot(fetch_submissions=False)
                self.assertEqual([task["name"] for task in snapshot["tasks"]], ["task-one"])
                self.assertEqual(snapshot["activeRoots"], [str(root1.resolve())])
                service.queue.set_root_active(str(root1), False)
                self.assertEqual(service.snapshot(fetch_submissions=False)["tasks"], [])
                service.queue.set_root_active(str(root2), True)
                self.assertEqual(
                    [task["name"] for task in service.snapshot(fetch_submissions=False)["tasks"]],
                    ["task-two"],
                )

    def test_new_root_is_inactive_until_enabled(self):
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp)
            root1 = base / "root-one"
            root2 = base / "root-two"
            root1.mkdir()
            root2.mkdir()
            config_path = base / "config.json"
            config = load_config(path=config_path, roots=[str(root1)])
            queue = QueueManager(config, JobManager(config), state_path=base / "queue.json")
            snapshot = queue.set_roots([str(root1), str(root2)])
            self.assertEqual(snapshot["roots"], [str(root1.resolve()), str(root2.resolve())])
            self.assertEqual(snapshot["activeRoots"], [str(root1.resolve())])
            snapshot = queue.set_root_active(str(root2), True)
            self.assertEqual(snapshot["activeRoots"], [str(root1.resolve()), str(root2.resolve())])

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

    def test_queue_start_cooldown_is_enforced_and_persisted(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_config(path=root / "config.json", roots=[str(root)])
            config["automation"]["paused"] = False
            config["automation"]["cooldownSeconds"] = 200
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": "platform-1", "source": "platform", "projectCode": "cy-1", "status": "pending"},
                            {"id": "platform-2", "source": "platform", "projectCode": "cy-2", "status": "pending"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            with mock.patch.object(monitor_core, "STATE_DIR", root):
                manager = QueueManager(config, JobManager(config), state_path=queue_path)
                manager._sync_running_locked = lambda: None
                starts = []

                def fake_start(item, *, reason="queue"):
                    starts.append(item["id"])
                    return {"pid": 1000 + len(starts), "startedAt": monitor_core.utc_now()}

                manager.jobs.start_platform = fake_start
                self.assertEqual(len(manager.tick()), 1)
                self.assertEqual(len(manager.tick()), 0)
                self.assertEqual(starts, ["platform-1"])
                manager._lastStartedAt = (datetime.now(timezone.utc) - timedelta(seconds=201)).isoformat().replace("+00:00", "Z")
                self.assertEqual(len(manager.tick()), 1)
                self.assertEqual(starts, ["platform-1", "platform-2"])
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["cooldownSeconds"], 200)
                self.assertIn("cooldownRemainingSeconds", snapshot)

    def test_queue_capacity_deduplicates_running_container_groups(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            config = load_config(path=root / "config.json", roots=[str(root)])
            config["automation"].update({
                "paused": False,
                "capacity": 2,
                "cooldownSeconds": 0,
                "startupTimeoutSeconds": 300,
            })
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps({
                    "items": [
                        {"id": "platform-1", "source": "platform", "projectCode": "cy-next-1", "status": "pending"},
                        {"id": "platform-2", "source": "platform", "projectCode": "cy-next-2", "status": "pending"},
                    ]
                }),
                encoding="utf-8",
            )

            class FakeDockerCache:
                @staticmethod
                def get():
                    return {
                        "error": "",
                        "items": [
                            {"name": "sologsb-cy-a-20260918-000000-candidate-1-1-a", "state": "running"},
                            {"name": "sologsb-cy-a-20260918-000000-candidate-2-1-b", "state": "running"},
                            {"name": "sologsb-cy-a-20260918-000000-candidate-3-1-c", "state": "running"},
                            {"name": "sologsb-cy-a-20260918-000000-candidate-1-0-old", "state": "exited"},
                        ],
                    }

            running_jobs = [
                {
                    "key": "platform:running-a",
                    "source": "platform",
                    "status": "running",
                    "taskRoot": str(root / "cy-a-20260918-000000"),
                    "startedAt": monitor_core.utc_now(),
                },
                {
                    "key": "platform:finalizing-b",
                    "source": "platform",
                    "status": "running",
                    "taskRoot": str(root / "cy-b-20260918-000000"),
                    "startedAt": "2020-01-01T00:00:00Z",
                },
            ]
            with mock.patch.object(monitor_core, "STATE_DIR", root):
                manager = QueueManager(
                    config,
                    JobManager(config),
                    state_path=queue_path,
                    docker_cache=FakeDockerCache(),
                )
                manager._sync_running_locked = lambda: None
                manager.jobs.running = lambda: [dict(job) for job in running_jobs]
                starts: list[str] = []

                def fake_start(item, *, reason="queue"):
                    starts.append(item["id"])
                    job = {
                        "key": f"platform:{item['id']}",
                        "source": "platform",
                        "status": "running",
                        "taskRoot": str(root / f"{item['projectCode']}-20260918-999999"),
                        "startedAt": monitor_core.utc_now(),
                        "pid": 2000 + len(starts),
                    }
                    running_jobs.append(job)
                    return job

                manager.jobs.start_platform = fake_start
                self.assertEqual(len(manager.tick()), 1)
                self.assertEqual(len(manager.tick()), 0)
                self.assertEqual(starts, ["platform-1"])
                snapshot = manager.snapshot()
                self.assertEqual(snapshot["capacityInUse"], 2)
                self.assertEqual(snapshot["counts"]["containerGroups"], 1)
                self.assertEqual(snapshot["counts"]["startupReservations"], 1)

    def test_successful_queue_item_is_removed_automatically(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_root = root / "real-task"
            (task_root / "monitor").mkdir(parents=True)
            (task_root / "monitor" / "state.json").write_text(
                json.dumps({"taskName": "real-task", "status": "complete", "sides": {}}),
                encoding="utf-8",
            )
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {"id": "done-1", "source": "platform", "status": "done", "taskRoot": str(task_root)},
                            {"id": "failed-1", "source": "platform", "status": "failed"},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            manager = QueueManager(load_config(roots=[str(root)]), JobManager(load_config(roots=[str(root)])), state_path=queue_path)
            snapshot = manager.snapshot()
            self.assertEqual([item["id"] for item in snapshot["items"]], ["failed-1"])
            self.assertTrue((task_root / "monitor" / "state.json").is_file())

    def test_platform_item_is_removed_after_successful_trigger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_root = root / "real-task"
            (task_root / "monitor").mkdir(parents=True)
            (task_root / "monitor" / "state.json").write_text(
                json.dumps({"taskName": "real-task", "status": "prepared", "sides": {}}),
                encoding="utf-8",
            )
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "platform-triggered",
                                "source": "platform",
                                "status": "running",
                                "jobPid": 12345,
                                "taskRoot": str(task_root),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result_file = root / "result.json"
            result_file.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "stage": "desktop-task-running",
                        "taskRoot": str(task_root),
                    }
                ),
                encoding="utf-8",
            )
            manager = QueueManager(load_config(roots=[str(root)]), JobManager(load_config(roots=[str(root)])), state_path=queue_path)
            manager.jobs.get_platform = lambda item_id: {
                "key": f"platform:{item_id}",
                "status": "running",
                "pid": 12345,
                "resultFile": str(result_file),
            }
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"], [])
            self.assertTrue((task_root / "monitor" / "state.json").is_file())

    def test_failed_worker_does_not_hide_successful_platform_trigger(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task_root = root / "real-task"
            (task_root / "monitor").mkdir(parents=True)
            (task_root / "monitor" / "state.json").write_text(
                json.dumps({"taskName": "real-task", "status": "candidates_running", "sides": {}}),
                encoding="utf-8",
            )
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "platform-triggered",
                                "source": "platform",
                                "status": "failed",
                                "taskRoot": str(task_root),
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result_file = root / "result.json"
            result_file.write_text(
                json.dumps(
                    {
                        "status": "running",
                        "stage": "desktop-task-running",
                        "taskRoot": str(task_root),
                    }
                ),
                encoding="utf-8",
            )
            manager = QueueManager(load_config(roots=[str(root)]), JobManager(load_config(roots=[str(root)])), state_path=queue_path)
            manager.jobs.get_platform = lambda item_id: {
                "key": f"platform:{item_id}",
                "status": "failed",
                "pid": 12345,
                "resultFile": str(result_file),
            }
            snapshot = manager.snapshot()
            self.assertEqual(snapshot["items"], [])
            self.assertTrue((task_root / "monitor" / "state.json").is_file())

    def test_submission_match(self):
        task = {"repoName": "cy402-hearing-schedule", "projectCode": "cy-402", "sides": {}}
        self.assertTrue(_submission_matches_task({"repo": "owner/cy402-hearing-schedule"}, task))
        self.assertTrue(_submission_matches_task({"prompt": "实现 cy-402 系统"}, task))
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
        self.assertIn("summary.activeTasks", html)
        self.assertIn(".mini-dot.running { border-color:var(--warn)", html)
        self.assertIn(".mini-dot.done { border-color:var(--ok)", html)
        self.assertIn(".mini-dot.failed, .mini-dot.stale { border-color:var(--bad)", html)
        self.assertIn('candidates_ready: "候选竞速结束"', html)
        self.assertIn('if (status === "cancelled") return "执行已停止";', html)
        self.assertIn('status === "cancelled" ? "×" : ""', html)
        self.assertIn('if (String(task.stateStatus || "") === "candidates_running") return true;', html)
        self.assertIn('function taskDurationInfo(task)', html)
        self.assertIn('const ENDED_TASK_STATUSES = new Set([', html)
        self.assertIn('class="task-duration ${duration.active ? "live" : ""}"', html)
        self.assertIn('data-command="dismiss-task"', html)
        self.assertIn('body: JSON.stringify({ action: "dismiss", taskId })', html)

    def test_summary_counts_tasks_not_parallel_candidate_instances(self):
        summary = MonitorService._summary([
            {
                "sides": {},
                "candidates": [
                    {"candidateId": "candidate-1", "active": True},
                    {"candidateId": "candidate-2", "active": True},
                    {"candidateId": "candidate-3", "active": True},
                ],
            }
        ])
        self.assertEqual(summary["activeTasks"], 1)
        self.assertEqual(summary["activeInstances"], 3)
        self.assertEqual(summary["activeSides"], 3)

    def test_summary_counts_active_codex_app_session_as_one_task(self):
        summary = MonitorService._summary([
            {
                "sides": {},
                "candidates": [],
                "active": True,
                "appSessions": [
                    {
                        "threadId": "01a0b071-cd01-7a63-93cb-f219ec74a7cf",
                        "status": "inProgress",
                    }
                ],
            }
        ])
        self.assertEqual(summary["activeTasks"], 1)
        self.assertEqual(summary["activeAppSessions"], 1)

    def test_dismiss_task_persists_and_hides_from_snapshot(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "case-dismiss"
            monitor = task / "monitor"
            monitor.mkdir(parents=True)
            (monitor / "state.json").write_text(
                json.dumps({"taskName": "case-dismiss", "status": "blocked", "sides": {}}, ensure_ascii=False),
                encoding="utf-8",
            )
            dismissed_path = root / "dismissed-tasks.json"
            with mock.patch.object(monitor_core, "DISMISSED_TASKS_PATH", dismissed_path):
                service = MonitorService(load_config(roots=[str(root)]))
                snapshot = service.snapshot(fetch_submissions=False)
                self.assertEqual(len(snapshot["tasks"]), 1)
                task_id = snapshot["tasks"][0]["id"]
                record = service.dismiss_task(task_id)
                self.assertEqual(record["taskId"], task_id)
                self.assertEqual(service.snapshot(fetch_submissions=False)["tasks"], [])
                self.assertIsNone(service.task_by_id(task_id))
                reloaded = MonitorService(load_config(roots=[str(root)]))
                self.assertEqual(reloaded.snapshot(fetch_submissions=False)["tasks"], [])

    def test_terminal_task_ignores_stale_app_session(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            task = root / "case-terminal"
            monitor = task / "monitor"
            monitor.mkdir(parents=True)
            (monitor / "state.json").write_text(
                json.dumps({"taskName": "case-terminal", "status": "complete", "sides": {}}),
                encoding="utf-8",
            )
            service = MonitorService(load_config(roots=[str(root)]))
            original = monitor_core.scan_codex_sessions
            monitor_core.scan_codex_sessions = lambda *args, **kwargs: [
                {
                    "threadId": "01a0b06c-6977-78e3-8fc8-1e52d513ad19",
                    "taskName": "case-terminal",
                    "status": "inProgress",
                    "active": True,
                }
            ]
            try:
                snapshot = service.snapshot(fetch_submissions=False)
            finally:
                monitor_core.scan_codex_sessions = original
            task_data = snapshot["tasks"][0]
            self.assertEqual(task_data["appSessions"], [])
            self.assertFalse(task_data["active"])
            self.assertEqual(snapshot["summary"]["activeTasks"], 0)

    def test_job_manager_recovers_live_running_jobs_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "jobs"
            state_dir.mkdir()
            log_path = state_dir / "20260918-120000-platform-platform-live.log"
            job = {
                "key": "platform:platform-live",
                "source": "platform",
                "platformItemId": "platform-live",
                "status": "running",
                "pid": 4242,
                "startedAt": "2020-01-01T00:00:00Z",
                "logPath": str(log_path),
            }
            log_path.with_suffix(".json").write_text(json.dumps(job), encoding="utf-8")
            with mock.patch.object(monitor_core, "STATE_DIR", root), mock.patch.object(
                monitor_core, "persisted_job_process_alive", return_value=True
            ):
                manager = JobManager(load_config(roots=[str(root)]))
                running = manager.running()
            self.assertEqual(len(running), 1)
            self.assertEqual(running[0]["key"], "platform:platform-live")
            self.assertEqual(running[0]["status"], "running")

    def test_stale_platform_worker_is_failed_and_releases_capacity(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "jobs"
            state_dir.mkdir()
            result_file = root / "result.json"
            task_root = root / "missing-task"
            result_file.write_text(
                json.dumps({
                    "status": "running",
                    "stage": "desktop-submitted",
                    "taskRoot": str(task_root),
                }),
                encoding="utf-8",
            )
            log_path = state_dir / "20260918-120000-platform-platform-stale.log"
            job = {
                "key": "platform:platform-stale",
                "source": "platform",
                "platformItemId": "platform-stale",
                "status": "running",
                "pid": 4242,
                "startedAt": "2020-01-01T00:00:00Z",
                "logPath": str(log_path),
                "resultFile": str(result_file),
            }
            log_path.with_suffix(".json").write_text(json.dumps(job), encoding="utf-8")
            command = f"python queue_worker.py --result-file {result_file}"
            with mock.patch.object(monitor_core, "STATE_DIR", root), mock.patch.object(
                monitor_core, "persisted_job_process_alive", return_value=True
            ), mock.patch.object(monitor_core, "pid_command", return_value=command), mock.patch.object(
                monitor_core.os, "killpg"
            ) as killpg:
                manager = JobManager(load_config(roots=[str(root)]))
                reaped = manager.reap_stale_platform_jobs(30)
            self.assertEqual(len(reaped), 1)
            self.assertEqual(manager.running(), [])
            self.assertEqual(reaped[0]["status"], "failed")
            self.assertIn("未创建任务目录", reaped[0]["error"])
            killpg.assert_called_once()

    def test_job_manager_reloads_running_platform_job_after_restart(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            state_dir = root / "jobs"
            state_dir.mkdir()
            log_path = state_dir / "20260918-120000-platform-platform-123.log"
            job_path = log_path.with_suffix(".json")
            job_path.write_text(
                json.dumps(
                    {
                        "key": "platform:platform-123",
                        "status": "running",
                        "pid": 0,
                        "logPath": str(log_path),
                    }
                ),
                encoding="utf-8",
            )
            manager = JobManager(load_config(roots=[str(root)]))
            original = monitor_core.STATE_DIR
            monitor_core.STATE_DIR = root
            try:
                job = manager.get_platform("platform-123")
            finally:
                monitor_core.STATE_DIR = original
            self.assertIsNotNone(job)
            self.assertEqual(job["status"], "failed")
            self.assertIn("进程已退出", job["error"])

    def test_pending_platform_item_recovers_when_persisted_worker_is_alive(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            queue_path = root / "queue.json"
            queue_path.write_text(
                json.dumps(
                    {
                        "items": [
                            {
                                "id": "platform-999",
                                "source": "platform",
                                "projectCode": "cy-999",
                                "status": "pending",
                                "jobPid": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            service = MonitorService(load_config(roots=[str(root)]))
            manager = QueueManager(service.config, service.jobs, state_path=queue_path)
            manager.jobs.get_platform = lambda item_id: {
                "key": f"platform:{item_id}",
                "status": "running",
                "pid": 12345,
                "startedAt": "2026-09-18T00:00:00Z",
                "resultFile": "",
            }
            manager._sync_running_locked()
            item = manager.snapshot()["items"][0]
            self.assertEqual(item["status"], "running")
            self.assertEqual(item["jobPid"], 12345)

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
