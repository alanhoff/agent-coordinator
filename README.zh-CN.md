<h1 align="center">Agent Coordinator</h1>
<p align="center">
  <a href="README.md">English</a>
  ·
  <a href="README.pt-BR.md">Português (Brasil)</a>
  ·
  <a href="README.es.md">Español</a>
  ·
  <a href="README.zh-CN.md">简体中文</a>
</p>
<p align="center"><strong>把复杂任务交给 Codex，获得清晰的计划和经过验证的结果。</strong></p>
<p align="center">让长任务始终易于理解、进度一目了然，并能在中断后回到经过验证的结果。</p>
<p align="center">
  <img src=".github/readme/agent-coordinator-hero.png" width="880" alt="一项复杂请求沿多条边界明确的工作路径推进，经过检查点后汇聚为一个经过验证的结果。">
</p>
<p align="center">
  <a href="#一条提示词完成安装"><strong>一条提示词完成安装</strong></a>
  ·
  <a href="#日常任务示例">查看日常任务示例</a>
</p>
<p align="center"><sub>MIT 许可 · 为当前用户账户安装 · 不更改 Codex 设置</sub></p>

## 你将获得

- 从提出请求到完成任务全程可跟踪的清晰计划。
- 责任明确的各个部分，每部分都有清晰的用途和负责人。
- 经过验证、可恢复且经得起中断的结果。

## Agent Coordinator 适合你吗？

| 适合使用的情况 | 通常无需使用的情况 |
|---|---|
| 工作涉及相互依赖的多个步骤、文件或专业领域。 | 任务只是一个明确的小步骤。 |
| 多个相互独立的部分可以安全地并行推进。 | 只需一个简短回答或微小修改。 |
| 一旦中断，就很难还原进度。 | 可以轻松地从原始提示词重新开始。 |

## 日常任务示例

> 为我的应用添加保存搜索功能，且不能破坏结账流程。

1. **明确完成标准：** 确定保存搜索的行为、结账流程的保护措施，以及各项内容的验证方式。
2. **让进度清晰易懂：** 将调研、针对性修改和回归检查分开，使每部分都有明确用途。
3. **完成前检查：** 审查修改过的文件和验证证据；若任务中断，则从已记录的进度继续，而不是从头开始。

只有保存搜索通过验收检查，且现有结账检查仍然通过，任务才算完成。

## 一条提示词完成安装

让 Codex 按照[仓库提供的安装流程](INSTALL.md)操作：

```text
按照 INSTALL.md 安装 https://github.com/alanhoff/agent-coordinator
```

该流程会将仓库克隆到临时目录，记录其提交 ID，为当前用户复制完整且自包含的 `skill/` 目录，删除临时检出目录，并报告安装路径和源提交。仅当现有目标目录能够表明自身属于 Coordinator 时，才会替换该目录。

运行 Coordinator 需要 Python 3.11 或更高版本，不需要第三方运行时软件包。安装过程不会修改 Codex 设置，也不会注册全局自定义智能体配置文件。

| 当前用户路径 | 存放内容 |
|---|---|
| `~/.agents/skills/coordinator` | 技能、角色配置文件、参考资料、Python 适配器和随附的运行时代码 |
| `~/.agent-coordinator` | 私有会话、锁、恢复数据和工作流状态 |

## 尝试第一个任务

在项目中发送以下入门提示词：

```text
$coordinator 审查本项目 README 中令人困惑的安装与配置步骤。不要修改文件。
返回影响最大的三项修正建议，分别引用依据，并确认没有修改任何文件。
```

成功的响应需满足三个条件：

1. 将三项修正建议按影响程度排序。
2. 每项建议都引用项目中的证据。
3. 响应确认没有修改任何文件。

## 工作原理

Coordinator 对每项任务都遵循以下四个步骤：

1. **理解：** 明确要求的结果、约束条件和成功证明。
2. **拆分：** 将任务分解为最小且有意义的部分，明确各自的边界和依赖关系。
3. **执行：** 按安全顺序处理已就绪的部分。专家智能体并非必需；如果无法使用，Coordinator 会直接通过同一流程逐一处理各部分。
4. **检查与恢复：** 检查结果及其证据，在重试前核实状态不确定的工作，只有所有要求均已满足或作出明确处置，且所有阻塞项均已解决后才结束任务。

持久化命令路径会将这些步骤编排为安全操作：

```text
plan-apply → next → node-route-auto → node-claim → node-start → node-complete
           ↘ refine/split/reconcile as required ↗
                         workflow-complete
```

`next` 是只读命令，它会报告下一个合法的操作类别，而不会嵌入完整的工作流状态。

## 常见问题

<details>
<summary>是否需要多个智能体？</summary>

不需要。有额外可用的智能体并发容量时，Coordinator 可以将相互独立的部分交给不同的智能体分别处理；否则，它会直接逐一完成这些部分。

</details>

<details>
<summary>是否需要显式调用？</summary>

需要。文档中的提示词模式要求每项协调任务都以 `$coordinator` 开头；安装操作只会放置技能，不会更改任何设置。

</details>

<details>
<summary>它会向我的项目或设置中添加什么？</summary>

它不会向目标项目添加由 Coordinator 持久保存的文件，也不会修改 Codex 设置或全局自定义智能体配置文件。初始化期间，它仅为检测仓库文件系统的大小写处理方式而创建并删除一个以私有名称命名的文件；在 Windows 上无需探测文件即可确定这一行为。

</details>

<details>
<summary>任务中断后会怎样？</summary>

新一次 Coordinator 运行可以从私有状态接续执行。它会标记可能仍在进行的工作，以便在重试前核实其状态；同时保留已完成的证据，并避免重复启动状态不确定的步骤。

</details>

## 参考

<details>
<summary>提示词模式</summary>

请使用显式的 `$coordinator` 前缀并说明完成标准。以下模式涵盖实现、诊断、恢复和仅基于证据的比较。

```text
$coordinator 交付保存搜索功能。检查仓库说明，保留验收标准，只拆分相互独立的工作，
验证集成后的行为，并以实质性证据结束任务。
```

```text
$coordinator 在修改生产代码前，复现并诊断间歇性失败的结账测试。将诊断、责任层的最小修复
和独立验证设为相互依赖的部分。
```

```text
$coordinator 恢复中断的 schema 迁移工作流。重试前核实状态不确定的工作，保留已完成的证据，
并验证回滚和正向迁移行为。
```

```text
$coordinator 根据当前仓库边界和要求比较拟议的事件处理架构。说明取舍和缺失的证据，
然后推荐一个方案，但不要实现任何一个方案。
```

</details>

<details>
<summary>评估与生命周期</summary>

Coordinator 会记录五个 0–4 级复杂度维度：广度、变更面、耦合度、新颖度和验证。它还会分别记录目标、输入、边界、依赖项和验收标准的 0–4 级歧义度。

达到默认阈值即会触发相应操作：复杂度总分达到 6 或任一维度达到 3 时必须拆分；歧义度总分达到 4 或任一因素达到 2 时必须细化。默认最大细化深度为 8。

```text
assess → refine or split → route → claim → execute → validate → reassess
```

开始路由前，每个未阻塞且可评估的叶节点都必须处于最新状态并可执行。要求或实际生效的依赖结果发生变化时，后续工作可能会过期，因此相关证据变化后会重新执行不动点检查。

</details>

<details>
<summary>所有权、路由与完成条件</summary>

- 每个可执行部分都有验收标准和一个角色，并可在 `write_scopes` 中声明零个或多个经过规范化的仓库相对路径。
- 未声明任何写入作用域表示仅收集证据的工作，并要求 `change_surface=0`。产物工作要求变更面评分为正数，且至少声明一个写入作用域。
- 处于活动状态且相互独立的工作，其写入作用域不得重叠。大小写比较遵循在目标文件系统上检测到的行为。
- 路由只会对当前运行时声明的候选项进行排名。如果没有可用的当前候选项清单，或选择失败，执行过程会继承父级模型和推理强度。
- 认领操作会为每个产物作用域记录 SHA-256 基线。完成时，要求声明的每个作用域仍然实际存在，并且在该次尝试期间已发生变化。
- 工作流只有在所有要求均已满足或作出明确处置、所有阻塞项均已解决、证据有效且所有状态都属于允许的终止状态时才能完成。Coordinator 不会调用或检查版本控制系统。

</details>

<details>
<summary>状态检查（包括 Windows）</summary>

持久化的 schema-v6 工作流文档位于 `~/.agent-coordinator/workflows`。以下命令均为只读，不会创建、锁定、修复、规范化、缓存或清理状态。

```sh
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py list --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py status --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py context --workflow-id WORKFLOW --json
python3 ~/.agents/skills/coordinator/scripts/coordinator_state.py next --workflow-id WORKFLOW --json
```

在 Windows 命令提示符中，请使用 `python` 和当前用户路径：

```bat
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" list --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" status --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" context --workflow-id WORKFLOW --json
python "%USERPROFILE%\.agents\skills\coordinator\scripts\coordinator_state.py" next --workflow-id WORKFLOW --json
```

Windows 状态位于 `%USERPROFILE%\.agent-coordinator\workflows`。

</details>

<details>
<summary>Docker 演示</summary>

该演示使用固定版本的 OpenAI Codex 通用镜像，并要求在被忽略的根目录 `.env` 文件中设置 `OPENAI_API_KEY`。技能和源代码以只读方式挂载；可变输出保存在被忽略的 `data/` 目录中。

使用 Coordinator 生成后端：

```sh
docker compose run --rm coordinator
```

`data/project/` 必须是全新的；唯一允许存在的顶层条目是一个现有的普通 `.nvmrc` 文件。再次运行前若需清空 `data/`，请先保留其中需要的内容。

生成的应用位于 `data/project/`，其后端位于 `data/project/backend/`。Codex 会话、Coordinator 状态和 SQLite 数据分别使用同级目录，生成的应用不纳入仓库自动化验证。

手动启动生成的后端：

```sh
docker compose up backend
```

随后即可通过 `http://localhost:3000` 访问 API，其 SQLite 数据库会持久保存在 `data/sqlite/todos.db`。

</details>

## 项目

- [MIT 许可证](LICENSE)
- [贡献指南](CONTRIBUTING.md)
- [安全政策](SECURITY.md)
- [GitHub 仓库](https://github.com/alanhoff/agent-coordinator)
- [公开问题跟踪器](https://github.com/alanhoff/agent-coordinator/issues)
