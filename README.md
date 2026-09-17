# sologsb-0917 任务监控

> 公开版不附带真实运行截图，避免泄露任务名、轨迹和命令输出。

`sologsb-0917` 的独立配套监控台，提供本地只读采集、显式操作的候选竞速与 A/B 双轨视图。默认扫描启动目录，或在 `config.json` / 页面中配置任务根目录。
竞速阶段展示 `candidate-1..N`，映射后再显示 A/B 两侧容器、SessionID、尝试次数、阶段、最近事件和静默时长。

## 启动

```bash
./run.sh
```

默认地址：<http://127.0.0.1:8790>

自动化任务管理页：<http://127.0.0.1:8790/tasks>

也可以指定多个扫描根目录或端口：

```bash
python3 server.py --root /path/to/task-root --port 8790
```

## 功能

- **项目屏蔽**：左侧项目可一键屏蔽，状态保存在当前浏览器；默认不展示，通过“管理已屏蔽”恢复。
- **独立任务管理页** `/tasks`：配置监控目录；本地任务可选择 A/B/both，新选中的平台任务固定走候选竞速；设置最大并发数后自动执行。
  支持暂停、调整顺序、失败重试、跳过已完成项和清理结束项。
- **自动触发 Prompt 模板**：模板保存在 `automation.promptTemplate`；平台任务入队时自动替换 `{{selected_project}}` 等占位符并保存本次 Prompt 快照，页面可直接复制。
- **候选竞速**：读取 `state.candidates` 和 `runtime/candidates/candidate-N`，实时展示 N 个独立候选；左侧按候选数显示对应亮点，中间为每个候选提供独立日志流。前两名按完成顺序映射 A/B，未进前两名主动停止。
- **A/B 双轨**：候选映射后固定显示两个逻辑侧，并标注其原始候选编号；文件夹不因映射改名。
- **历史尝试**：按 SessionID 合并 runtime 与 rejected 轨迹，重跑后会列出全部尝试，
  不会因为 `attempt-01` 编号复用而覆盖旧记录。
- **项目提示词**：从任务的 `monitor/state.json` 指向的真实文件读取，页面直接展示全文。
- **TodoWrite**：解析容器内 Claude Code 的真实 `TodoWrite` 事件，展示待办、进行中和已完成项；
  TodoWrite 仅用于进度可视化，不作为完成或发布门禁。
- **真实进度**：增量读取 Claude Code `stdout.jsonl`，最近进展区域按尝试分段并可滚动；
  展示模型输出、工具调用、错误、命令数和静默时长，不伪造完成百分比。
- **容器状态**：通过 `docker ps -a` 匹配 `attempt.json` 中的容器名；状态为空时明确显示。
- **手动续跑**：A、B 可单独续跑或并行续跑，严格调用 `sologsb.py run`。
- **技能托管重试**：自动重试、异常恢复和失败现场处理由 `sologsb-0917` 技能负责；
  监控台不提供自动续跑或强制重跑入口。
- **SOLO2 提交**：优先读取本机 `solo2-monitor:8787` 的缓存接口；若其未运行，再从 macOS
  Keychain 读取凭据直连 SOLO2。Cookie 和 CSRF 不落盘、不返回前端。
- **审计轨迹**：每次监控端续跑都会写入 `sologsb-monitor/.state/jobs/*.log`，页面可展开查看。

## 续跑语义

页面上的“续跑”只调用技能 CLI，不直接操作容器或任务状态。技能内部是否重试、
如何重试以及失败现场如何处理，全部由 `sologsb-0917` 自己决定：

```text
首轮竞速   -> sologsb.py run --task-root ROOT --side both --candidates 3 --attempts 6
续跑 A/B   -> sologsb.py run --task-root ROOT --side A|B
并行续跑   -> sologsb.py run --task-root ROOT --side both
```

监控台不提供自动续跑和强制重跑入口；候选竞速进行中会禁用并行续跑按钮。

## 环境变量

- `SOLO_MANAGER_BASE_URL`：Solo Manager 地址；留空时禁用平台项目查询。
- `SOLO2_SERVER`：SOLO2 地址；留空时禁用直连提交信息查询。
- `SOLO2_KEYCHAIN_SERVICE`：SOLO2 凭据的 Keychain 前缀，默认 `sologsb-qa`。
- `SOLOGBS_MONITOR_ROOT`：未配置 `roots` 时的默认扫描目录。

## 配置

配置文件是 [`config.json`](./config.json)。相对任务目录按启动进程当前目录解析；建议使用本机绝对路径。

常用字段：

- `roots`：任务扫描根目录列表。
- `skillScript`：留空时使用 `$CODEX_HOME/skills/sologsb-0917/scripts/sologsb.py`；也可显式配置绝对路径。
- `server.host` / `server.port`：监听地址与端口。
- `server.allowRemoteActions`：是否允许局域网客户端执行续跑，默认 `false`。
- `automation.capacity`：自动化队列最大并发任务数。
- `automation.paused`：队列是否暂停，默认暂停。
- `automation.tickSeconds`：调度轮询间隔。
- `automation.promptTemplate`：自动触发任务的 Prompt 模板，支持 `{{selected_project}}`、`{{project_code}}`、`{{project_name}}`、`{{task_type}}`、`{{difficulty}}`。
- `roots`：监控目录列表，页面可直接增删。
- `solo2.monitorUrl`：可选的本地缓存服务地址。
- `solo2.enabled`：设为 `false` 可关闭提交信息。

## API

```text
GET  /api/health
GET  /api/snapshot?submissions=1&refresh=0
GET  /api/automation
GET  /api/platform/projects?taskType=0-1代码生成&refresh=0
POST /api/automation  {"action":"set-roots|set-capacity|set-paused|queue-add|queue-remove|queue-move|queue-retry|queue-clear", ...}
GET  /api/history?taskId=...&side=A&limit=500
GET  /api/log?taskId=...&side=A&kind=events|job&lines=200
POST /api/action  {"taskId":"...","side":"A","mode":"resume|rerun|both"}
POST /api/auto    {"enabled":true,"taskId":"..."}  # taskId 省略时为全局开关
```

POST 操作默认只接受本机请求。如果确实需要局域网操作，先自行确认网络环境，再设置
`server.allowRemoteActions=true`。

## 测试

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile server.py monitor_core.py
```
