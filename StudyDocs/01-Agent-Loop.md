# 01 Agent Loop 核心循环

## 第一部分：总结介绍

Agent Loop 是 coding agent 的核心执行循环。普通聊天机器人通常只接收用户输入并生成文本，而 Agent Loop 会让模型在每一轮响应中决定是否需要调用工具。如果模型返回普通文本，说明它认为当前任务已经完成；如果模型返回 `tool_use`，宿主程序就负责执行工具，把执行结果包装成 `tool_result`，再作为新的消息放回上下文，继续调用模型。这个循环让模型从“只会说”变成“可以读文件、搜代码、执行命令、编辑文件”的执行型助手。

在 Python 版本里，主入口是 `/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/agent.py:452` 的 `Agent.chat()`。它会在第一次聊天时懒加载 MCP 工具，然后根据后端选择 `_chat_openai()` 或 `_chat_anthropic()`。Anthropic 版本的主循环在 `/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/agent.py:1487`。它先把用户消息加入 `_anthropic_messages`，然后进入 `while True`：调用模型、收集 `tool_use`、保存 assistant 回复、如果没有工具调用就结束；如果有工具调用，就权限检查、执行工具、处理大结果，然后把所有工具结果作为 `role: "user"` 的 `tool_result` 消息塞回历史，进入下一轮。

这个设计里有一个关键点：循环是否继续不是代码靠关键词判断，而是模型通过结构化工具调用决定。宿主程序不写“如果用户说读文件就读文件”的业务分支，而是把工具能力披露给模型，让模型选择工具。程序负责执行、权限、安全和状态维护。这样一来，Agent 的能力可以通过新增工具扩展，而不是不断写自然语言意图识别分支。

Python 代码里的异步写法也服务于这个循环。`async def chat(...) -> None` 表示这是一个异步函数，调用模型、连接 MCP、执行部分工具都可能等待 IO。`await` 表示等待这个异步操作完成后再继续。例如 `await self._mcp_manager.load_and_connect()` 保证 MCP 连接完成后再获取工具定义；`await coro` 保证真正的 `_chat_anthropic()` 或 `_chat_openai()` 循环跑完后再清理当前任务并保存会话。可以把 Python 的 coroutine 理解成 JavaScript 的 Promise：它代表未来会完成的异步结果。

工具返回结果也不是无限制塞进上下文。`_persist_large_result()` 在 `/Users/8utterf1y/Desktop/agent项目/claude-mini/claude-code-from-scratch/python/mini_claude/agent.py:1181` 负责处理大工具结果。如果结果不超过 30KB，就原样返回；如果超过 30KB，就把完整结果保存到 `~/.mini-claude/tool-results/`，只把文件路径和前 200 行预览放进 `tool_result`。这样既避免上下文被大日志撑爆，又不丢失完整信息，模型后续仍然可以用 `read_file` 读取保存下来的完整结果。

### 面试话术版本

这个项目的 Agent Loop 本质上是一个“模型决策、程序执行、结果回填”的循环。用户输入进入消息历史后，Agent 调用模型。如果模型只返回文本，当前轮结束；如果模型返回 `tool_use`，Agent 就执行对应工具，把结果包装成 `tool_result` 再放回消息历史，继续调用模型。Python 版本里这个逻辑主要在 `Agent._chat_anthropic()` 中实现。它不是靠手写规则判断用户意图，而是把工具 schema 发给模型，由模型决定何时调用工具，宿主程序负责权限、执行、安全和上下文管理。为了支持网络和 IO，外层使用 `async/await`；为了控制上下文，工具结果过大时会落盘，只把预览和路径回填给模型。

## 第二部分：面试问答与追问补充

### Q1：什么是 Agent Loop？

Agent Loop 是 Agent 的核心运行机制：调用模型，检查模型是否请求工具，执行工具，把工具结果回填到消息历史，再次调用模型，直到模型不再请求工具。它让模型具备和外部环境交互的能力。

### Q2：Agent 和普通 Chatbot 的区别是什么？

普通 Chatbot 主要生成文本；Agent 可以调用工具改变或读取外部环境。这个项目里模型可以请求 `read_file`、`grep_search`、`run_shell`、`edit_file` 等工具，宿主程序实际执行，再把结果交给模型继续推理。

### Q3：循环什么时候停止？

当模型响应里没有工具调用时停止。在 Anthropic 路径里就是 `tool_uses = [b for b in response.content if b.type == "tool_use"]` 后，如果 `not tool_uses` 就 `break`。

### Q4：为什么工具结果要重新放回 messages？

模型不会自动知道本地工具执行结果。工具是在宿主程序里执行的，执行完必须把结果包装成 `tool_result` 放回消息历史，下一轮模型调用才能基于真实结果继续推理。

### Q5：为什么 Anthropic 工具结果用 `role: "user"`？

这是 Anthropic 工具调用协议的消息格式要求。模型先发出 assistant 消息里的 `tool_use`，宿主程序必须在下一条 user 消息中放入对应的 `tool_result`。这里的 user 不是用户本人输入，而是协议层面的工具结果回传。

### Q6：`tool_result` 里是完整工具输出吗？

不一定。小结果会完整放入 `tool_result.content`；超过 30KB 的大结果会先由 `_persist_large_result()` 保存到本地文件，`tool_result.content` 只包含保存路径和前 200 行预览。

### Q7：`_persist_large_result()` 为什么要先落盘再截断？

因为如果先截断，完整结果就丢了。项目先把完整输出写入 `~/.mini-claude/tool-results/`，再生成预览并做安全截断。这样模型上下文不会爆掉，同时后续仍可通过 `read_file` 读取完整内容。

### Q8：`async def`、`await` 在这里有什么作用？

`async def` 声明异步函数，返回 coroutine；`await` 等待异步操作完成。Agent 需要连接 MCP、调用 LLM、执行异步工具，这些都可能等待 IO。`await` 让代码看起来像同步顺序执行，但底层不会阻塞事件循环。

### Q9：Python 的 coroutine 和 JavaScript 的 Promise 有什么相似点？

二者都代表“未来会完成的异步结果”。JavaScript 里 `async function` 返回 `Promise`，Python 里 `async def` 返回 coroutine。都需要用 `await` 获取最终结果。

### Q10：为什么 `Agent.chat()` 里要保存 `_current_task`？

它用于中断控制。`chat()` 执行期间记录当前 asyncio task，用户中断时可以取消当前任务；结束或异常后在 `finally` 中清空，避免状态残留。

### Q11：MCP 工具为什么在第一次 chat 时懒加载？

MCP 连接可能涉及外部进程或网络，没必要在 Agent 初始化时就连接。第一次真正聊天时加载，可以减少启动成本，也避免子 Agent 重复初始化主 Agent 的外部工具。

### Q12：Agent Loop 的核心状态是什么？

核心状态是消息历史，例如 `_anthropic_messages` 和 `_openai_messages`。它们记录用户输入、assistant 回复、工具调用和工具结果。模型每一轮决策都依赖这些历史。

### Q13：为什么要把 assistant 的完整回复保存，而不是只保存文本？

因为 assistant 回复里可能包含 `tool_use`。后续 `tool_result` 必须通过 `tool_use_id` 与之前的工具调用配对。如果只保存文本，会破坏工具调用协议。

### Q14：预算超限时为什么还要给每个工具调用补一个结果？

因为每个 `tool_use` 都必须有对应 `tool_result`，否则下一次 API 调用的消息历史是无效的。预算超限时项目会给每个工具调用补一个“未执行”的工具结果。

### Q15：面试时如何一句话总结这个循环？

Agent Loop 就是让模型持续做“观察上下文、决定是否调用工具、读取工具结果、继续推理”的闭环；模型负责决策，程序负责执行和安全边界。

