---
title: 智能面试平台系统设计
description: 介绍项目目标、总体架构、核心模块、关键技术决策和风险边界
tags: [系统设计, 架构, 面试, RAG, 异步任务]
---

# 01. 智能面试平台系统设计

## 1.1 项目目标

智能面试平台用于根据候选人简历和企业知识库生成面试题，记录作答过程，并生成可追溯的评估报告。系统优先保证题目与候选人经历相关，同时要求知识库答案带有来源引用。

核心目标不是简单调用 LLM，而是把“上传资料 -> 解析资料 -> 建立知识索引 -> 生成问题 -> 收集答案 -> 评估反馈”串成稳定链路。每个链路都要有状态记录、失败重试和可观测日志，避免一次模型调用失败导致整场面试不可恢复。

## 1.2 用户角色

系统面向三类用户：

- 候选人：上传简历，参加文本或语音面试，查看最终反馈。
- 面试官：配置岗位要求、知识库和题库偏好，查看候选人表现。
- 管理员：维护租户配置、模型 Provider、对象存储和异步 Worker。

候选人侧更关注流程顺畅，面试官侧更关注题目质量和证据引用，管理员侧更关注成本、稳定性和数据隔离。

## 1.3 总体架构

系统分为 Web 前端、API 服务、异步任务、对象存储、PostgreSQL 和 LLM Provider 六个部分。API 服务处理用户与面试会话；异步任务处理文件解析、Embedding 和报告生成；PostgreSQL 使用 pgvector 保存知识切片向量。

```text
Browser -> API Gateway -> Interview Service
                    |-> Resume Service -> Object Storage
                    |-> Knowledge Service -> PostgreSQL/pgvector
                    |-> Task Stream -> Worker -> LLM Provider
                    |-> Evaluation Service -> Report Repository
```

API 服务只做短事务，不在 HTTP 请求里同步解析 PDF 或调用 Embedding。耗时任务全部投递到 Redis Stream，由 Worker 消费并写回任务状态。

## 1.4 核心模块

- `resume`：上传、解析、分析和管理简历。
- `knowledgebase`：切片、Embedding、检索和引用组装。
- `interview`：生成题目、管理会话和保存答案。
- `evaluation`：根据评分规则产生结构化评价。
- `infrastructure/file`：为简历和知识库提供共享文件基础设施。
- `llm`：封装多 Provider 调用、超时、重试和结构化输出校验。
- `task`：封装 Redis Stream 的投递、确认、重试和死信处理。

## 1.5 数据流

知识库上传后先进入 `PENDING` 状态，文件基础设施完成 MIME 检测、hash 去重和对象存储写入。随后异步任务读取对象存储中的原始文件，调用解析器提取文本，按章节或段落切片，批量调用 Embedding Provider，并把向量写入 pgvector。

面试生成问题时，系统先根据岗位、简历和知识库范围构造查询，再进行混合检索。向量检索负责语义相关，关键词检索负责专有名词、接口名、配置项和错误码。两路结果用 RRF 合并后再 rerank，最终只把 top 3 到 top 5 的证据交给 LLM。

## 1.6 关键决策

知识库检索使用关键词与向量混合召回，再使用 rerank 选出最相关的证据。文件处理放入异步 Worker，避免 PDF 解析和 Embedding 阻塞请求线程。原始文件保存到 S3 兼容对象存储，数据库只保存对象键、状态和索引元数据。

LLM 输出必须经过 JSON Schema 校验。题目生成、答案评分和报告总结都不能直接信任自然语言输出。如果结构化解析失败，系统会带着错误原因进行一次修复请求；仍然失败则进入可重试状态。

## 1.7 可靠性边界

系统允许单个文件解析失败，但不能影响同一租户的其他知识库。系统允许某个 Worker 离线，但未确认任务必须能被其他 Worker 接管。系统允许 LLM 超时，但面试会话状态不能丢失。

所有异步任务都有 `task_id`、`attempt_count`、`last_error` 和 `next_retry_at`。用户看到的是业务状态，开发者看到的是任务状态，两者不能混在一起。

## 1.8 面试表达重点

面试时不要只说“我用了 RAG”。更好的表达是：项目把文件解析、切片、Embedding、混合召回、rerank、引用组装和答案约束连成完整闭环，并且通过任务状态和 active version 解决失败恢复问题。

如果被问为什么不用纯向量检索，可以回答：纯向量对语义问题有效，但对 MIME、SHA-256、pgvector、XAUTOCLAIM 这类精确术语不稳定，所以需要 FTS 或 BM25 参与召回，再用 RRF 融合。
