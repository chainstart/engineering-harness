# Engineering Harness

Engineering Harness 是一个面向生产级软件开发的工程母机：它把目标、规格、路线图、状态、策略、执行器、测试、报告、提交和恢复机制组织成一套可持续运行的软件开发工作流，让 AI 编程能力可以围绕同一个工程目标长期、自循环、可审计地推进。

它不是为某个本地 `work` 目录服务的脚本集合，也不是用来快速生成玩具 demo 的模板工具。它的目标是成为通用的软件工程智能体控制平面：用户给出需求、规格或总目标，Harness 将目标拆成路线图和任务，调用可替换的执行器完成实现，运行验收与端到端验证，记录证据，管理状态，在失败时修复或暂停，在成功时提交和推送，并在无人值守模式下持续推进下一阶段。

适用对象不限制领域，也不限制软件形态。它可以服务网站、App、游戏、智能体、后端服务、CLI、数据和研究系统、嵌入式程序、Verilog/HDL、EDA 流程、形式化验证、协议工程、CI/CD 和运维自动化。真正的边界来自当前大模型的软件工程能力、目标项目的工具链成熟度，以及验证与安全策略能否给出可靠反馈。

## Core Positioning

Engineering Harness 的核心不是“再造一个编码模型”，而是给编码模型和工程工具提供生产级工作流。

- **目标驱动**：所有工作围绕用户需求、规格、蓝图和路线图展开，而不是围绕一次性 prompt。
- **路线图驱动**：项目状态由 `.engineering/roadmap.yaml`、任务阶段、验收命令和延续阶段描述。
- **无人值守**：`drive`、`self-iterate` 和 `daemon-supervisor` 支持长时间运行、预算控制、空闲退避和断点恢复。
- **可替换执行器**：Shell、Codex、未来的模型 API、CI runner、Dagger、OpenHands、SWE-agent 或硬件工具链都应该只是执行器。
- **生产级闭环**：实现、测试、修复、E2E、报告、策略决策、git checkpoint、推送和 CI/CD 应该成为同一条闭环。
- **记忆和状态优先**：长期运行需要状态文件、决策日志、报告、manifest、审批记录和失败隔离，而不是依赖一次对话上下文。
- **安全与治理内建**：文件范围、命令策略、秘密脱敏、网络和部署能力、人工审批、审计证据必须在执行前后被记录和约束。

## What It Is Not

Engineering Harness 不应被理解为：

- 只服务某个本地目录下项目的个人脚本。
- 只生成网页、玩具项目或演示 demo 的脚手架。
- 单一 AI coding assistant 的替代品。
- 单纯的 CI 系统、任务队列或项目管理工具。
- 只适合 Python、前端或区块链项目的垂直工具。

它应该是这些系统之上的工程控制层：把大模型、代码仓库、测试、CI、部署、知识库和人工审批组织成一个可持续的自治开发系统。

## Current Capabilities

当前版本已经具备这些基础能力：

- 初始化项目级 `.engineering/` 目录和内置 profile。
- 从高层目标生成 starter roadmap。
- 执行 roadmap task 的 `implementation`、`acceptance`、`repair`、`e2e` 阶段。
- 调用 Shell 和 gated Codex executor。
- 维护 durable state、phase history、decision log、Markdown report 和 JSON manifest。
- 支持 rolling continuation、自迭代 planner、pause/resume/cancel 和 approval gate。
- 支持命令 allowlist、live/manual/agent gate、file scope guard、unsafe capability audit 和 secret redaction。
- 支持任务成功后的 git commit/push checkpoint。
- 生成或检查项目 experience plan，并为 UI/API/CLI 体验创建 E2E-oriented 任务。
- 支持 workspace dispatch 和 daemon supervisor，让多个项目可以按策略轮转执行。

这些能力仍然是工程母机的骨架。后续重点是让它更像一个长期工作的自治工程组织，而不只是本地 CLI。

## Mental Model

每个目标项目拥有自己的工程控制目录：

```text
.engineering/
  roadmap.yaml
  policies/
    command-allowlist.yaml
    deployment-policy.yaml
    secret-policy.yaml
  state/
    harness-state.json
    decision-log.jsonl
  reports/
```

核心循环如下：

```text
goal/spec/blueprint
  -> roadmap
  -> next task
  -> implementation executor
  -> acceptance checks
  -> repair loop
  -> e2e/user or hardware validation
  -> report + manifest + state
  -> git checkpoint / push / CI
  -> continuation or self-iteration
```

任务是 Harness 的最小自治单元。一个任务通常包含：

- `file_scope`：执行器允许修改的文件范围。
- `implementation`：让 agent 或脚本完成实现。
- `acceptance`：判定任务是否完成的测试或检查。
- `repair`：验收失败后的修复步骤。
- `e2e`：用户路径、系统路径、硬件仿真或集成验证。
- `max_task_iterations`：实现/验收/修复循环的上限。

## Relation To Devin

Devin 公开定位是 AI software engineer：能写、运行、测试代码，并通过 Web、IDE、Shell、Browser、API、Slack/Teams、GitHub/GitLab/Bitbucket、Linear/Jira、scheduled sessions、playbooks 和 session insights 等方式进入团队工作流。参考：[Introducing Devin](https://docs.devin.ai/get-started/devin-intro)、[Scheduled Sessions](https://docs.devin.ai/product-guides/scheduled-sessions)、[Session Insights](https://docs.devin.ai/product-guides/session-insights)。

Engineering Harness 可以借鉴 Devin 的方向，但定位不同：

- Devin 更像云端或托管的 AI 工程师产品；Engineering Harness 更像开源、可本地运行、执行器可替换的工程控制平面。
- Devin 面向“把任务交给一个 AI 工程师”；Engineering Harness 面向“把长期软件开发流程制度化，让不同执行器持续完成路线图”。
- Devin 强调交互式接管、团队集成和托管体验；Engineering Harness 应强调路线图 schema、状态机、策略、证据、断点重续、执行器插件和可审计自治。
- Devin 的强项包括 task delegation、parallel backlog work、scheduled sessions、knowledge/playbooks、session insights 和现成集成；Engineering Harness 应把这些思想拆成开放模块，而不是绑定到单一平台。

可借鉴方向：

- **Session insights**：为每次 drive/session 生成健康度、失败原因、上下文质量、token/成本、重试次数和改进建议。
- **Playbooks/skills**：把可复用工作流封装成版本化 playbook，例如“修复 CI”、“升级依赖”、“实现 API endpoint”、“Verilog 仿真闭环”。
- **Scheduled sessions**：把 daemon supervisor 提升为正式调度系统，支持 cron、队列、优先级、通知和历史审计。
- **Knowledge onboarding**：建立项目知识库索引，吸收 `README`、spec、ADR、issue、PR、CI 日志和设计文档。
- **Team integrations**：接入 GitHub/GitLab、Jira/Linear、Slack/Teams、CI、artifact registry 和部署平台。
- **Takeover experience**：提供本地 dashboard 或 Web UI，让人可以观察、暂停、批准、接管和恢复任务。

## Development Direction

要把 Engineering Harness 发展成真正的软件工程智能体，下一步应优先补齐这些层：

1. **Roadmap schema 和 migration**
   - 为目标、规格、任务、依赖、风险、预算、验收、E2E、硬件仿真、部署和审批建立稳定 schema。
   - 提供 schema version、迁移工具、golden fixtures 和兼容性测试。

2. **Executor plugin system**
   - 将 Shell、Codex、OpenAI API、Dagger、GitHub Actions、OpenHands、SWE-agent、Verilator、Vivado、PlatformIO 等都建模为 executor。
   - 每个 executor 声明能力、成本、隔离级别、网络/文件/秘密需求和结果契约。

3. **Model and memory layer**
   - 支持多模型路由、成本预算、上下文压缩、项目知识索引、长期记忆和任务级 prompt 模板。
   - 把 memory 存成可审计资产，而不是只留在模型会话里。

4. **Durable autonomous runtime**
   - 强化 daemon、lease、heartbeat、stale recovery、retry backoff、并发调度和跨项目队列。
   - 支持真正 24/7 运行：可暂停、可恢复、可升级、可审计、可限制预算。

5. **Evaluation and production acceptance**
   - 除单元测试外，支持 browser E2E、API journey、CLI journey、load test、security scan、HDL simulation、formal verification、hardware-in-loop 和部署烟测。
   - 让“完成”的定义来自证据，而不是来自模型自评。

6. **CI/CD and release automation**
   - 自动生成或维护 CI workflow。
   - 将本地 manifest 映射到 PR comment、commit status、artifact 和 release notes。
   - 支持失败 CI 回流到 roadmap task。

7. **Security and governance**
   - 继续强化 unsafe capability classification、secret redaction、sandbox policy、deployment gate、approval queue 和 audit log。
   - 对生产环境、资金、私钥、客户数据、硬件烧录等高风险操作默认拒绝或要求人工批准。

8. **Operator UI**
   - 提供本地或托管 dashboard，展示目标、路线图、运行中任务、失败隔离、审批、报告、E2E 证据和资源消耗。
   - UI 应服务工程运维，不做营销式页面。

9. **Domain packs**
   - 为 Web/App/Game/Agent/Embedded/Verilog/Formal/Data/DevOps 等领域提供 profile、验收模板、工具链检测和 playbook。
   - Harness 保持通用，领域能力通过 profile、executor 和 playbook 扩展。

## Installation

从仓库根目录安装为 editable 包：

```bash
python3 -m pip install -e .
```

安装后可以使用 CLI：

```bash
engh --help
```

不安装时也可以直接运行：

```bash
PYTHONPATH=src python3 -m engineering_harness.cli --help
```

## Built-In Profiles

查看内置 profile：

```bash
engh profiles
```

当前内置：

- `agent-monorepo`
- `evm-protocol`
- `evm-security-research`
- `lean-formalization`
- `node-frontend`
- `python-agent`
- `trading-research`

这些 profile 只是起点，不是领域边界。嵌入式、Verilog、游戏、移动端、数据平台等方向应通过后续 domain packs 扩展。

## Initialize A Project

为任意目标项目初始化工程控制目录：

```bash
engh init \
  --project-root /path/to/project \
  --profile python-agent \
  --name my-project
```

验证 roadmap：

```bash
engh validate --project-root /path/to/project
```

查看状态：

```bash
engh status --project-root /path/to/project
engh status --project-root /path/to/project --json
```

## Create A Roadmap From A Goal

从目标生成 starter roadmap：

```bash
engh plan-goal \
  --project-root /path/to/project \
  --name my-project \
  --profile python-agent \
  --goal "Build a production-grade autonomous research agent with durable task state and operator dashboard."
```

写入 `.engineering/roadmap.yaml`：

```bash
engh plan-goal \
  --project-root /path/to/project \
  --name my-project \
  --profile python-agent \
  --goal-file docs/goal.md \
  --blueprint docs/spec.md \
  --materialize
```

如果 roadmap 已存在并且确认要替换，添加 `--force`。

## Run One Task

查看下一个任务：

```bash
engh next --project-root /path/to/project
```

试运行，不执行真实命令：

```bash
engh run --project-root /path/to/project --dry-run
```

执行下一个任务：

```bash
engh run --project-root /path/to/project
```

如果任务需要调用 coding agent，需要显式允许：

```bash
engh run --project-root /path/to/project --allow-agent
```

## Autonomous Drive

持续运行 pending tasks，直到任务完成、失败、阻塞或预算耗尽：

```bash
engh drive \
  --project-root /path/to/project \
  --max-tasks 5 \
  --time-budget-seconds 14400
```

允许 rolling continuation：

```bash
engh drive \
  --project-root /path/to/project \
  --rolling \
  --time-budget-seconds 14400
```

允许自迭代 planner 在路线图耗尽后追加下一阶段：

```bash
engh drive \
  --project-root /path/to/project \
  --rolling \
  --self-iterate \
  --allow-agent \
  --time-budget-seconds 14400
```

任务通过后自动 checkpoint：

```bash
engh drive \
  --project-root /path/to/project \
  --rolling \
  --allow-agent \
  --commit-after-task
```

同时推送：

```bash
engh drive \
  --project-root /path/to/project \
  --rolling \
  --allow-agent \
  --commit-after-task \
  --push-after-task
```

## 24/7 Supervisor

`daemon-supervisor` 用于长时间轮转 workspace 中的项目：

```bash
engh daemon-supervisor \
  --workspace /path/to/workspace \
  --rolling \
  --self-iterate \
  --allow-agent \
  --run-window-seconds 86400 \
  --sleep-seconds 30 \
  --scheduler-policy fair
```

它会反复执行 `workspace-drive` tick，按预算和调度策略选择项目，记录 runtime 状态，并在空闲或连续无产出时退避。

## Pause, Resume, Cancel, Approve

暂停未来调度：

```bash
engh pause --project-root /path/to/project --reason "operator review"
```

恢复：

```bash
engh resume --project-root /path/to/project --reason "review complete"
```

取消未来调度，直到恢复：

```bash
engh cancel --project-root /path/to/project --reason "stop this run"
```

查看审批队列：

```bash
engh approvals --project-root /path/to/project
```

批准所有 pending gates：

```bash
engh approve --project-root /path/to/project --all --reason "approved by operator"
```

## Frontend, API, CLI, And E2E Experience

生产级软件必须定义用户或操作员如何验证它已经可用。Harness 使用 `experience` block 描述目标体验：

```json
{
  "experience": {
    "kind": "dashboard",
    "personas": ["operator"],
    "primary_surfaces": ["run queue", "artifact viewer", "failure dashboard"],
    "auth": {
      "required": false,
      "roles": []
    },
    "e2e_journeys": [
      {
        "id": "operator-inspects-run",
        "persona": "operator",
        "goal": "Inspect a completed autonomous run and review its artifacts."
      }
    ]
  }
}
```

生成体验相关任务：

```bash
engh frontend-tasks --project-root /path/to/project
engh frontend-tasks --project-root /path/to/project --materialize
```

这里的 “frontend” 不只等于 Web UI。它也可以是 API journey、CLI journey、硬件仿真报告、EDA waveform artifact、operator dashboard 或任何真实用户/工程师用来判断系统完成度的界面。

## Roadmap Continuation

当显式任务耗尽时，可以让 Harness materialize 下一批 continuation stages：

```json
{
  "continuation": {
    "enabled": true,
    "goal": "Ship the full production system described by the spec.",
    "blueprint": "docs/spec.md",
    "stages": [
      {
        "id": "production-hardening",
        "title": "Production hardening",
        "objective": "Add reliability, observability, and release checks.",
        "tasks": [
          {
            "id": "production-hardening-tests",
            "title": "Add production readiness checks",
            "file_scope": ["src/**", "tests/**", "docs/**"],
            "acceptance": [
              {
                "name": "focused tests",
                "command": "python3 -m pytest tests/test_production_readiness.py -q"
              }
            ]
          }
        ]
      }
    ]
  }
}
```

手动推进：

```bash
engh advance --project-root /path/to/project
```

在 drive 中自动推进：

```bash
engh drive --project-root /path/to/project --rolling
```

## Task Example

```json
{
  "id": "worker-runtime-loop",
  "title": "Implement durable worker runtime loop",
  "max_task_iterations": 3,
  "file_scope": ["src/**", "tests/**", "docs/**"],
  "implementation": [
    {
      "name": "Codex implementation",
      "executor": "codex",
      "prompt": "Implement the durable worker loop described by this task.",
      "timeout_seconds": 3600
    }
  ],
  "acceptance": [
    {
      "name": "focused tests",
      "command": "python3 -m pytest tests/test_worker_runtime.py -q"
    }
  ],
  "repair": [
    {
      "name": "Codex repair",
      "executor": "codex",
      "prompt": "Fix the failing acceptance checks for this task.",
      "timeout_seconds": 1800
    }
  ],
  "e2e": [
    {
      "name": "operator journey",
      "command": "python3 -m pytest tests/e2e/test_operator_journey.py -q"
    }
  ]
}
```

## Safety Model

默认原则：生产自治必须先能停下来、解释自己、留下证据。

- Coding agent executor 默认受 gate 保护，需要 `--allow-agent`。
- Live operations、部署、主网、资金、私钥、外部写操作和高风险删除必须经过策略和人工审批。
- 命令执行会记录 policy decisions、capability classification 和 safety audit。
- 报告、manifest 和状态中的敏感值会脱敏。
- file scope guard 会阻止任务越界修改。
- checkpoint readiness 会区分干净工作区、Harness 生成改动和无关用户改动。

安全策略不应阻止生产开发，而应把风险显式化：什么可以自动执行，什么必须等待人工批准，什么永远不应该由无人值守 agent 直接执行。

## Documentation

更多设计文档：

- [Autonomous Engineering Harness Development Plan](docs/autonomous-engineering-harness-plan.md)
- [Durable Drive Controls](docs/durable-drive-controls.md)
- [Executor Contract](docs/executor-contract.md)
- [Goal Intake Contract](docs/goal-intake-contract.md)
- [Goal Roadmap Planner](docs/goal-roadmap-planner.md)
- [Policy Engine V2](docs/policy-engine-v2.md)
- [Browser User Experience E2E](docs/browser-user-experience-e2e.md)
- [Workspace Drive Dispatcher](docs/workspace-drive-dispatcher.md)

## License

许可证待定。公开生产发行前应添加明确的开源许可证。
