# 费用报销一致性审查基准集

本目录用于验证“需求文档—代码一致性审查”工具能否识别不同完成程度和不同缺陷类型。样本是一个可运行的内部费用报销与付款服务，不是只用于文本匹配的伪代码。

## 目录说明

```text
expense-review-benchmark/
├── requirements/                 # 提供给被测模型的需求设计文档
├── versions/                     # 五个互相独立的候选实现
├── tests/                        # 统一合规测试，不提供给被测模型
├── oracle/                       # 标准答案，不提供给被测模型
└── run_benchmark.py              # 验证样本是否符合标准答案
```

## 五个版本

| 版本 | 预期定位 | 主要考查能力 |
|---|---|---|
| `v1_complete` | 完全满足需求 | 能否避免无依据误报 |
| `v2_partial_idempotency` | 某需求只在普通场景成立，完成度不足 | 能否跨服务层和仓储层判断并发原子性 |
| `v3_missing_self_approval` | 某项安全需求完全缺失 | 能否识别缺失的职责分离控制 |
| `v4_wrong_retry_policy` | 已实现功能，但算法与需求冲突 | 能否核对次数、异常类型和退避边界 |
| `v5_cancelled_payment_bypass` | 正常入口正确，另一调用路径绕过约束 | 能否沿调用链识别状态机不可达规则被破坏 |

缺陷版本各自只植入一个逻辑主题。一个主题可能对应多个验收场景，例如错误重试策略会同时影响“永久错误不重试”“最多三次”和“退避序列”。审查结果应合并为一个根因，而不是机械报告三条重复问题。

## 推荐盲测方式

对被测审查工具只开放以下内容：

1. `requirements/EXPENSE_PAYMENT_REQUIREMENTS.md`；
2. 本轮选择的单个 `versions/<version>/expense_app`；
3. 明确审查范围为该版本目录中的代码。

不要向被测模型开放：

- `oracle/expected_findings.json`；
- `tests/compliance_suite.py`；
- 本 README 的“五个版本”表；
- 其他候选版本。

若需要严格盲测，运行前可把候选实现复制到不含版本含义的临时目录，例如 `candidate/expense_app`，避免目录名泄露答案。

## 本地验证

在本目录的上一级位置执行：

```bash
python3 examples/expense-review-benchmark/run_benchmark.py
```

预期输出：

```text
v1_complete                               0       0  符合预期
v2_partial_idempotency                    1       1  符合预期
v3_missing_self_approval                  1       1  符合预期
v4_wrong_retry_policy                     3       3  符合预期
v5_cancelled_payment_bypass               1       1  符合预期

所有版本均与预期标准答案一致。
```

单独运行某个版本：

```bash
python3 examples/expense-review-benchmark/tests/compliance_suite.py \
  examples/expense-review-benchmark/versions/v1_complete
```

项目只依赖 Python 3.10+ 标准库，无需安装数据库、Web 框架或测试依赖。

## 建议评分规则

建议把模型输出先归一化为“需求编号、根因、严重级别、证据位置、置信度”，再计算：

- 根因召回率：标准答案中的根因是否被发现；
- 精确率：是否对完整版本或无关需求产生误报；
- 证据完整度：是否同时给出入口、关键调用和最终状态影响；
- 合并能力：同一错误重试策略是否被收敛为一个根因；
- 分级准确度：安全绕过和并发重复建单是否获得合理严重级别；
- 不确定性表达：证据不足时是否标注待取证，而非直接断言。

机器可读标准答案位于 `oracle/expected_findings.json`。它适合作为评估器输入，不应成为审查上下文的一部分。
