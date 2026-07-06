# Token Governance Layer 整体方案

## 1. 总体判断

我们要做的不是“又一个压缩器”，而是一个面向开发者 AI Agent 的 **Token Governance Layer（Token 治理层）**。

它的核心目标是：在节省 token 的同时，保证任务质量、上下文可信度和可追溯性。也就是说，它不只是把内容压短，而是决定：

- 什么内容值得进入上下文。
- 什么内容可以摘要。
- 什么内容必须保持原文。
- 什么内容可以延迟加载。
- 压缩后如何证明没有删掉关键事实。
- 必要时如何找回原文。
- token 节省应该归因到哪个任务、工具、模型、MCP server 或会话阶段。

现有工具已经证明“token 浪费”是真问题，但它们大多只解决局部问题：MCP schema 太大、tool output 太长、会话历史太臃肿、文件读取太粗暴、日志太吵、成本不可见。我们的机会是把这些点串成一个统一的治理层。

## 2. 竞品与痛点脉络

### 2.1 MCP / Tool Schema 压缩

代表项目：

- [mcp-compressor](https://github.com/atlassian-labs/mcp-compressor)
- [TSCG](https://github.com/SKZL-AI/tscg)
- [mcp-slim](https://github.com/dopatools/mcp-slim)
- [MCP token bloat SEP-1576](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/1576)

它们主要解决 MCP 工具列表和 JSON Schema 的“启动税”。大型 MCP server 可能暴露几十到几百个工具，每个工具都带名称、描述、参数 schema、嵌套 JSON Schema。Agent 每轮都背着这些信息，会大量浪费上下文。

已有方案的优点：

- 工具 schema 可以按需加载。
- 模型先看到压缩后的工具目录，再请求具体工具的完整 schema。
- 对多工具 MCP server 的 token 节省很明显。

已有痛点：

- 主要处理 `tools/list`，不处理后续 tool output、文件读取、日志、会话历史。
- 多 MCP server 组合后，路由和工具选择仍然复杂。
- 代理层如果实现不稳，会影响整个 Agent 工作流。
- 用户需要知道被隐藏的工具是否真的可用，以及为什么某个工具没有进入上下文。

### 2.2 CLI / Tool Output 压缩

代表项目：

- [RTK](https://github.com/rtk-ai/rtk)
- [Headroom](https://github.com/headroomlabs-ai/headroom)
- [Token Optimizer MCP](https://github.com/ooples/token-optimizer-mcp)
- [LeanCTX](https://github.com/yvgude/lean-ctx)

它们处理开发者 Agent 最常见的高频浪费：`ls`、`tree`、`cat`、`grep`、`rg`、`git diff`、`git status`、测试输出、构建日志、长 JSON、RAG chunk 等。

已有方案的优点：

- 对日常开发命令输出的节省非常直接。
- 能减少 Agent 读取整文件、整日志、整 diff 的冲动。
- 有些工具支持本地缓存和原文取回。

已有痛点：

- 如果改写命令语义，会产生严重风险。例如开发服务器命令、搜索命令、多路径命令一旦被错误处理，Agent 会基于错误事实继续推理。
- 压缩后的内容如果没有原文 ID、hash、receipt 和恢复工具，用户无法验证“省 token 没省掉真相”。
- 很多工具关注 token 数字，但不证明任务质量没有下降。
- 对不同内容类型的保护策略不够细，例如失败断言、错误栈、命令参数、文件路径、代码符号不应该被随意摘要。

### 2.3 会话压缩与上下文清理

代表方向：

- Claude Code 的自动压缩与手动 `/compact`
- [Claude Code auto compact issue](https://github.com/anthropics/claude-code/issues/66144)
- [Claude Code /handover issue](https://github.com/anthropics/claude-code/issues/54254)
- Cozempic、context-pruner、memory keeper 类工具

这类方案解决长会话里历史消息、重复 tool result、旧文件读取、thinking block、截图、日志、低相关记忆不断堆积的问题。

已有方案的优点：

- 能避免会话到达上下文上限后突然停止。
- 能把长期事实、决策和进度从聊天历史里提取出来。
- 部分方案可以在上下文到达危险阈值前主动清理。

已有痛点：

- 用户最担心“删错”。一旦摘要丢失关键决策，后续任务质量会明显下降。
- `/compact` 这类自动摘要通常不可控，用户不能明确选择哪些内容必须保留。
- 老会话恢复后仍可能加载臃肿历史，并没有真正降低上下文压力。
- 单靠提示词要求 Agent “自己压缩”不可靠，因为压缩触发属于运行时行为，不是模型指令能稳定控制的。

### 2.4 精确代码检索与按需上下文

代表项目：

- [jCodeMunch](https://github.com/jgravelle/jcodemunch-mcp)
- [Claude Context](https://github.com/zilliztech/claude-context)
- [Context7](https://github.com/upstash/context7)
- RepoMix 类工具

它们的核心思路是：不要让 Agent 粗暴读取整个仓库、整个文件、整套文档，而是按 symbol、chunk、文档版本、语义搜索结果提供最小上下文。

已有方案的优点：

- 能显著减少整文件读取。
- 更适合大型代码库。
- 能让 Agent 先搜索，再精确读取相关函数、类、常量、接口或文档片段。

已有痛点：

- 索引漂移、旧 snapshot、重复 chunk、vector DB 配置错误会让上下文“看起来对，实际错”。
- 用户需要知道检索结果来自哪个索引版本、是否过期、是否重复、是否被客户端截断。
- 隐私场景下，不能为了 observability 直接记录原始代码、prompt 或完整 transcript。

### 2.5 成本观测与归因

代表方向：

- AgentTrace
- Burnd
- ai-token-exporter
- WhereMyTokens 类工具

它们主要回答“token 花在哪里了”。

已有方案的优点：

- 能统计 session、工具、模型、请求、成本。
- 能发现高消耗工具、重复调用和异常膨胀。

已有痛点：

- 多数是事后报表，不能在下一轮调用前治理上下文。
- 能指出哪里烧钱，但不能自动给出低风险替代方案。
- 缺少与压缩策略、上下文质量、任务结果之间的闭环。

## 3. 产品定位

### 3.1 一句话定位

**Token Governance Layer 是一个本地优先的上下文治理层，帮助开发者 AI Agent 在不牺牲任务质量的前提下，减少无效上下文、降低 token 成本，并保留可验证、可恢复的证据链。**

### 3.2 核心承诺

- 节省 token，但不盲目追求极限压缩。
- 对关键事实保守，对噪音积极压缩。
- 每次治理动作都可解释、可审计、可恢复。
- 不把 mock 数据、假健康状态、错误摘要伪装成真实上下文。
- 不默认破坏原始工具语义。

### 3.3 差异化

市面上很多工具强调“减少 60%-95% token”。我们应该强调：

- **可证明的节省**：每次 before / after 有明确 token 计量。
- **可恢复的真相**：压缩内容有 receipt 和原文取回路径。
- **可配置的策略**：团队可以定义哪些内容永不压缩。
- **可归因的成本**：知道 token 被 MCP schema、文件、日志、diff、测试、历史会话分别消耗了多少。
- **可验证的质量**：通过基准任务确认 Agent 仍能完成任务，而不是只看 token 数字。

## 4. 核心架构

### 4.1 MCP Gateway Layer

位置：Agent 与 MCP servers 之间。

职责：

- 拦截 `tools/list`。
- 将完整工具列表压缩成轻量工具目录。
- 根据任务上下文选择相关工具。
- 只在需要时暴露具体工具完整 schema。
- 记录每次工具 schema 暴露的 token 成本。

核心能力：

- 多 MCP server 聚合。
- 工具分组。
- 懒加载 schema。
- 工具选择 receipt。
- 禁用或隐藏无关工具。

### 4.2 Tool Output Shaper

位置：工具调用结果返回 Agent 之前。

职责：

- 识别内容类型：日志、diff、测试输出、JSON、代码、搜索结果、文件内容、命令输出。
- 对噪音做结构化摘要。
- 对关键事实保持原样。
- 为被压缩内容生成 receipt。
- 将原文存入本地 ledger。

示例：

- 测试输出保留失败测试名、断言、实际值、期望值、文件路径、行号。
- 长日志按错误簇、时间段、重复模式压缩。
- `git diff` 保留文件列表、hunk 摘要、关键变更片段。
- 大 JSON 保留 schema、关键字段、异常字段、样本路径。

### 4.3 Context Ledger

位置：本地 SQLite 或等价轻量数据库。

职责：

- 存储原始 payload。
- 存储压缩 payload。
- 存储 receipt。
- 存储 token before / after。
- 存储 hash、时间、来源工具、session、task、policy。
- 支持按 receipt ID 找回原文。

原则：

- 默认本地。
- 默认不上传。
- 原文可设置 retention。
- receipt 不应泄露敏感原文。

### 4.4 Policy Engine

职责：

- 决定哪些内容可压缩、如何压缩、何时压缩。
- 为不同内容类型设置保护规则。
- 在上下文预算接近阈值时触发治理动作。
- 为每次动作打风险等级。

默认策略应该保守：

- 用户指令不压缩。
- 安全、权限、密钥相关内容不做语义改写。
- 错误栈、失败断言、命令参数、文件路径、代码符号默认保留原文。
- 长重复日志、目录树、重复搜索结果、历史低相关上下文优先压缩。
- 无 receipt 的压缩不得进入默认路径。

### 4.5 Restore Interface

面向 Agent 和用户提供恢复与解释能力。

建议 MCP tools：

- `retrieve_original(receipt_id)`：取回原始内容。
- `explain_receipt(receipt_id)`：解释压缩了什么、为什么、节省多少。
- `expand_summary(receipt_id, focus)`：围绕某个焦点展开摘要。
- `show_savings(scope)`：查看 session、task、tool、server 的节省。
- `list_context_risks(scope)`：列出高风险压缩或不确定摘要。

### 4.6 Observability CLI / Dashboard

职责：

- 展示 token 消耗来源。
- 展示压缩收益。
- 展示高风险上下文。
- 展示 restore 频率。
- 展示哪些工具、文件、命令最烧 token。

建议 CLI：

- `tgl doctor`
- `tgl stats`
- `tgl receipts`
- `tgl inspect <receipt_id>`
- `tgl policy validate`

## 5. V1 范围

### 5.1 优先集成形态

V1 优先做 **本地 MCP Gateway / MCP Server**，原因：

- 对 Claude Code、Codex、Cursor、VS Code、Windsurf 等 MCP client 更通用。
- 不需要一开始侵入具体 Agent 内部实现。
- 可以同时治理 tool schema 和 tool output。
- 便于做本地存储、receipt、restore。

可选增强：

- Codex 插件。
- Claude Code hooks。
- Shell wrapper。
- SDK middleware。

这些不作为第一阶段必须项。

### 5.2 V1 支持内容类型

- MCP tool definitions / JSON schema。
- Shell / MCP tool output。
- 文件读取结果。
- `git status`、`git diff`、`git log`。
- 测试、lint、build 输出。
- 长日志。
- 长 JSON。
- 会话摘要与 handover 草稿。

### 5.3 V1 不做

- 不做云同步。
- 不做团队计费 SaaS。
- 不做任意 LLM API 生产流量代理。
- 不做激进不可逆压缩。
- 不改写命令语义。
- 不直接删除用户 transcript。
- 不默认做跨机器记忆。

## 6. 对外接口草案

### 6.1 MCP Tools

```text
govern_context(payload, content_type, policy)
```

对输入上下文执行治理，返回压缩内容、receipt ID、token 节省和风险等级。

```text
retrieve_original(receipt_id)
```

按 receipt ID 取回原始内容。

```text
explain_receipt(receipt_id)
```

解释治理动作，包括来源、策略、压缩前后 token、hash、保留字段、删除/摘要内容类型。

```text
show_savings(scope)
```

按 session、task、tool、MCP server、content type 查看 token 节省。

```text
list_context_risks(scope)
```

列出高风险摘要、不可恢复内容、过期索引、低置信检索结果。

### 6.2 CLI

```bash
tgl doctor
tgl stats
tgl receipts
tgl inspect <receipt_id>
tgl policy validate
```

### 6.3 配置文件

建议文件名：

```text
token-governance.config.json
```

核心配置：

- MCP servers。
- compression thresholds。
- protected content types。
- tokenizer / model mapping。
- receipt retention。
- local database path。
- per-tool policy。
- context budget threshold。
- restore behavior。

## 7. 技术路线图

### 阶段 1：Research Pack

目标：把现有调研沉淀成可维护竞品矩阵。

产出：

- 竞品列表。
- 功能矩阵。
- 集成方式。
- 声称节省比例。
- 用户痛点。
- 风险分析。
- 我们的对应设计机会。

验收：

- 至少覆盖 MCP schema 压缩、tool output 压缩、session pruning、代码检索、成本观测五类。
- 每类至少 2-3 个代表项目。

### 阶段 2：最小 MCP Gateway 原型

目标：先证明 tool schema 治理可行。

能力：

- 代理一个 MCP server。
- 拦截 `tools/list`。
- 返回压缩工具目录。
- 支持按需获取完整工具 schema。
- 记录 schema token before / after。

验收：

- 对同一个 MCP server，对比原始工具列表和治理后工具目录的 token。
- Agent 仍能调用目标工具。

### 阶段 3：Receipt-Based Output Shaping

目标：让 tool output 压缩可恢复、可解释。

能力：

- 本地 ledger。
- receipt ID。
- 原文 hash。
- 原文取回。
- token before / after。
- 内容类型识别。

验收：

- 长日志、测试输出、文件读取、diff 都能生成 receipt。
- 任意 receipt 都可以取回原文。
- receipt 能解释节省和风险。

### 阶段 4：Policy Engine

目标：从“压缩函数”升级成“治理策略”。

能力：

- 默认保守策略。
- 内容类型策略。
- 工具级策略。
- 保护字段。
- 风险等级。
- 阈值触发。

验收：

- 用户指令、失败断言、错误栈、命令参数、文件路径、代码符号默认不被语义改写。
- 长重复日志、目录树、重复搜索结果优先压缩。
- 高风险压缩会被标记。

### 阶段 5：开发者工作流验证

目标：证明省 token 不影响任务完成。

方法：

- 选取真实开发任务。
- 分别运行 raw agent 和 governed agent。
- 对比 token、耗时、工具调用、任务成功率、测试结果、恢复次数。

验收：

- 有明确 token 节省。
- 任务结果不下降。
- 出现不确定上下文时可以通过 restore 解决。

### 阶段 6：打包成插件 / MCP 工具

目标：让开发者能安装使用。

优先：

- 本地 MCP package。
- 简单 CLI。
- 安装文档。
- 示例配置。

之后再考虑：

- Codex 插件。
- Claude Code hooks。
- dashboard。
- SDK middleware。

## 8. 测试计划

### 8.1 Token 节省测试

- 原始 MCP tool schema vs 压缩工具目录。
- 原始 shell output vs 治理后 output。
- 原始 test / lint / build 输出 vs 结构化摘要。
- 原始 diff / log vs 治理后摘要。

指标：

- input token before。
- input token after。
- savings percentage。
- receipt count。
- restore count。

### 8.2 任务质量测试

- Agent 能否定位失败测试。
- Agent 能否修复 bug。
- Agent 能否正确理解 diff。
- Agent 能否避免读取无关大文件。
- Agent 能否在需要时主动取回原文。

指标：

- 任务完成率。
- 测试通过率。
- 错误修复轮数。
- 错误上下文导致的失败次数。

### 8.3 安全与可信测试

- 用户指令不被压缩成错误语义。
- 失败断言行号不丢失。
- 文件路径不被改写。
- 命令参数不被改写。
- receipt hash 能检测原文变化。
- 过期索引或过期上下文会被标记。

### 8.4 兼容性测试

- Windows。
- macOS。
- Linux。
- 至少一个 MCP client。
- 至少一个 coding-agent CLI。
- 多 MCP server。
- 大型工具 schema。
- 大型日志输出。

## 9. 关键原则

- 最大目标不是极限压缩，而是可信节省。
- 压缩必须可解释。
- 压缩必须可恢复。
- 关键事实默认保护。
- 工具语义不应被代理层偷偷改变。
- Observability 不能泄露敏感原文。
- 事后报表不够，治理必须发生在上下文进入模型之前。
- Agent 不应该自己猜哪些上下文重要，治理层需要提供结构化策略。

## 10. 初始默认假设

- V1 本地优先。
- V1 面向开发者 coding agent。
- V1 首选 MCP Gateway 形态。
- V1 不做云服务。
- V1 不做不可逆激进压缩。
- V1 从 Claude Code / Codex 风格工作流切入。
- V1 成功标准是：可量化节省 token，同时任务质量不下降，且压缩内容可追溯、可恢复。

