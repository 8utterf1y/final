# 朱子墨 AI 面试补充问答：运行时与协议层

这份补充问答主要覆盖运行时、CLI、流式协议、双后端、Session 等偏底层实现问题。它们更像加分项，不是简历主线，适合在面试官继续深挖系统实现时使用。

## 一、REPL / CLI 作为统一控制面

### 1. 面试官问：REPL 在这个项目里到底意味着什么？

REPL 不只是一个输入框，而是 Agent 的统一交互入口。它先判断用户输入属于哪一类，再决定走本地命令链路还是模型执行链路。

如果是普通任务描述，就进入 `agent.chat()`；如果是 `/clear`、`/cost`、`/compact`、`/goal`、`/kb`、`/skills` 这类控制命令，就直接由 CLI 本地处理。这样做的核心价值是把“系统控制操作”和“开放语义任务”分层，避免把本来确定可执行的控制行为也交给模型理解。

### 2. 面试官问：你为什么说 CLI 是统一控制面，而不只是聊天壳？

因为它不仅负责收消息，还负责参数解析、权限模式选择、会话恢复、本地命令分发、中断处理和 REPL 生命周期管理。也就是说，CLI 这一层掌握的是系统运行方式，而不是任务语义本身。

更准确地讲，CLI 更像 control plane，Agent 更像 execution plane。前者决定系统如何运行，后者负责模型推理、工具调用、上下文治理和任务收敛。

### 3. 面试官问：为什么不把 `/clear`、`/compact` 这种命令也交给模型？

因为这类命令本质上不是开放任务，而是确定性的系统操作。直接在 CLI 层处理有三个好处：第一，行为稳定，不依赖模型理解；第二，不消耗 token；第三，避免模型误解导致操作偏离预期。

所以我把本地命令设计成应用层控制协议，而不是一段让模型自己猜意思的 Prompt。

### 4. 面试官问：CLI 启动后到 Agent 退出，执行链路怎么描述？

可以这样理解：`main` 先完成参数解析和初始化，然后创建并进入 `asyncio` event loop；事件循环里运行 `run_repl()`，持续接收用户输入。每当用户提交一个任务，REPL 会通过 `await agent.chat()` 把控制权交给 Agent，之后由 event loop 调度模型流式请求、MCP 通信和本地工具任务。

任务完成后控制权回到 REPL，继续等待下一轮输入；用户退出时，再统一关闭 MCP 连接和异步资源，最后结束 event loop。它不是线性脚本，而是一个以 REPL 为外层、以 `agent.chat()` 为核心入口、以 `asyncio` 为调度器的异步系统。

## 二、双后端与流式协议适配

### 5. 面试官问：双后端支持具体指什么？支持 OpenAI 还是 Anthropic？

严格说是两类都支持。一个是 Anthropic 原生后端，另一个是 OpenAI-compatible 后端。

这里第二类不只是 OpenAI 官方，也包括兼容 OpenAI Chat Completions 协议的网关或模型服务。项目里通过 CLI 参数和初始化配置在两条链路之间切换，上层 Agent 生命周期保持一致，底层分别做协议适配。

### 6. 面试官问：双后端具体差异有哪些？

主要有八个差异点。第一，SDK 不同，Anthropic 用 `AsyncAnthropic`，OpenAI-compatible 用 `AsyncOpenAI`。第二，请求协议不同，Anthropic 是 `system + messages + tools` 和 content block，OpenAI-compatible 是 `messages + tool_calls`。第三，工具 schema 不同，所以要做一层工具定义转换。第四，流式事件格式不同，Anthropic 有 `content_block_start/delta/stop`，OpenAI-compatible 是 chunk delta。

第五，工具结果回填协议不同，Anthropic 用 `tool_result` block，OpenAI-compatible 用 `role=tool`。第六，前缀缓存机制不同，Anthropic 可以显式做 cache breakpoint，OpenAI-compatible 更多依赖提供方自动缓存。第七，thinking 支持方式不同，当前实现只在 Anthropic 路径显式开启。第八，usage 和成本统计字段不同，所以 token 统计逻辑也是分开的。

### 7. 面试官问：为什么不把两种协议强行统一成一套消息格式？

因为统一抽象不等于强行抹平差异。两边的消息结构、工具调用和工具结果协议本来就不一样，如果先转换成中间格式，再在保存、恢复、压缩和回放时双向翻译，反而更容易丢字段和破坏配对关系。

所以我的做法是：上层统一 Agent 行为层，比如任务循环、权限控制、工具执行、上下文治理；底层分别保留 `_anthropic_messages` 和 `_openai_messages` 两套原生历史。这样协议差异被隔离在适配层，运行期行为更稳定。

### 8. 面试官问：Anthropic 流式协议里你具体处理了什么？

Anthropic 的关键在于 content block 事件流。实现上会处理 `content_block_start`、`content_block_delta` 和 `content_block_stop` 三类事件。

文本增量可以直接实时输出；工具参数则通过 `partial_json` 在内存里持续拼接。等到 `content_block_stop` 到达时，说明这个 `tool_use` 块已经完整闭合，这时才能把 JSON 解析成真正的工具调用。最后还要拿一次完整 `final_message` 入历史，因为终端显示的零散文本不能直接作为可靠上下文。

### 9. 面试官问：流还没结束，为什么可以提前执行工具？

因为在 Anthropic 路径里，一个 tool block 可能已经完整结束，但整轮响应还没结束。这时如果工具是并发安全、无副作用、权限已自动允许，就可以提前启动，让工具执行和模型后续生成重叠，缩短总等待时间。

但这个提前执行边界必须很严。只有 `CONCURRENCY_SAFE_TOOLS`、权限静态允许、且 Auto Mode 下属于 fast-path 的工具才能这样做。否则一旦流中途失败并重试，就可能重复触发副作用。

### 10. 面试官问：OpenAI-compatible 为什么要手动拼工具参数？

因为 OpenAI-compatible 的工具参数经常被拆成多个 chunk 返回，中间状态通常不是合法 JSON。比如第一片只到 `"{"file"`，第二片才补全剩余字段。

所以实现上要先按 tool call 的 `index` 维护中间状态，持续拼接 `arguments` 字符串，等流结束后再统一解析和重建完整 tool call。这也是为什么 OpenAI-compatible 路径更适合“先收完整响应，再做权限检查和执行”。

### 11. 面试官问：为什么 Anthropic 可以更激进地流式提前执行，而 OpenAI-compatible 不这么做？

因为两边的流式结构不对称。Anthropic 明确给出了 tool block 的起止边界，所以我能知道某个工具调用什么时候已经完整闭合；OpenAI-compatible 更多是增量拼接参数，直到流后段才知道 JSON 是否真正完整。

所以 Anthropic 更适合边流边启动安全工具，OpenAI-compatible 更适合在完整响应后再进入权限检查和执行阶段。这不是能力高低问题，而是协议粒度不同导致的调度策略不同。

## 三、权限模式从 CLI 到 Agent 的落地

### 12. 面试官问：权限模式是怎么从 CLI 进入 Agent 的？

CLI 启动时会先根据参数解析出这次会话的权限模式，比如 `--yolo` 对应 `bypassPermissions`，`--plan` 对应 `plan`，默认是 `default`。这一步只是“选模式”，不是实际做权限判定。

真正的 `allow`、`deny`、`confirm` 发生在 Agent 处理每一次工具调用时。也就是说，CLI 层决定“这次会话按什么策略运行”，Agent 层决定“这一次工具调用到底放不放行”。前者是配置入口，后者才是安全执行逻辑。

### 13. 面试官问：为什么不直接在 CLI 层把权限一次性判完？

因为权限判断依赖的是具体工具、具体参数和具体上下文，而这些只有到了运行时工具调用发生时才完整可见。CLI 启动阶段只知道会话模式，不知道模型后面会不会读文件、改文件、跑 Shell，或者访问哪个路径。

所以更合理的分层是：CLI 负责选策略，Agent 在每次 tool call 上按策略落规则。这样既保留统一入口，也保留细粒度安全控制。

## 四、Session 与状态恢复

### 14. 面试官问：Session 保存和加载的意义是什么？

Session 本质上是会话级状态持久化。它让 Agent 不是一次性脚本，而是可中断、可恢复、可追踪的交互系统。

当前实现保存的是基础元数据和后端原生消息历史，而不是只存一段文本摘要。这样恢复时能直接把上下文接回去，继续后续任务，而不是从一个模糊 summary 重新开始。

### 15. 面试官问：Session 具体保存什么？

主要是两类信息。第一类是 `metadata`，包括 session id、模型名、当前工作目录、开始时间和消息数。第二类是消息历史，Anthropic 路径保存 `anthropicMessages`，OpenAI-compatible 路径保存 `openaiMessages`。

也就是说，这里保存的是模型真正继续对话所需的结构化上下文，而不是只保存用户看得到的聊天文本。

### 16. 面试官问：为什么 Session 不能只保存纯文本对话？

因为 Agent 对话里不只有自然语言，还有结构化工具调用和工具结果。如果只保存纯文本，恢复时模型就失去了哪些工具已经调过、哪些结果已经返回、哪些配对关系必须保留这些关键信息。

尤其在双后端下，Anthropic 的 `tool_use/tool_result` 和 OpenAI-compatible 的 `tool_calls/role=tool` 协议不一样，只存文本会把很多运行期语义抹掉，所以必须保存结构化消息历史。

### 17. 面试官问：Session 为什么不是完整 checkpoint？

因为当前目标是做“上下文可恢复”，不是“进程态完全回放”。如果真做完整 checkpoint，你还要持久化挂起任务、工具执行状态、权限确认缓存、MCP 连接状态、memory prefetch 状态等，这些恢复成本更高，也不一定稳定。

所以这版设计有意把范围控制在高收益、可落地的一层：会话上下文恢复。这样既能支持长任务续跑，也不会把系统复杂度一下拉得太高。

## 五、Session / TaskJournal / Experience 的边界

### 18. 面试官问：Session 和任务轨迹、经验库是什么关系？

三者的职责不同。Session 负责恢复当前会话上下文，任务轨迹负责保留程序侧证据流，经验库负责把有复用价值的处理过程提炼成长期知识。

所以 Session 更偏“继续当前任务”，TaskJournal 更偏“记录真实执行证据”，Experience 更偏“沉淀可复用流程”。把这三层拆开后，恢复、评测和知识复用不会混在一起。
