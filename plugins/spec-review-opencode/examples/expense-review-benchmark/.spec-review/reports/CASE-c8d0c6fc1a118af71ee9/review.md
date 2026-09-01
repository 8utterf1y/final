# 需求—代码一致性审查报告

- 案例 ID：`CASE-c8d0c6fc1a118af71ee9`
- 审查模式：`auto`
- 基准版本：`未提供`
- 目标版本：`0101b531adf6af94136bab1b6b7a9ec10718aef6`
- 审查类型：`snapshot`
- 覆盖率：`115/115`

## 审查结论

审查完成：consistent=106，inconsistent=7，uncertain=0，not_applicable=2。

## 问题清单

### 1. CLAIM-5fad4b5a3c9de6c9c367

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：背景目标#5：付款开始前允许取消，且后台任务不得给已取消单据付款。 代码行为：PayoutWorker.process（application.py:182-214）直接 transition({APPROVED,CANCELLED}->PAYING) 后调用网关，CANCELLED 单可被付款。 候选差异：承诺'防止已取消单据被后台任务付款'，代码允许 CANCELLED->PAYING->网关。

证据：`EVID-d7430315a03e42406651`, `EVID-49bb88a87d3d368c969c`, `EVID-4eb3127975259fb0ad2b`, `EVID-2cbab8600cda55a0038b`, `EVID-25302ba17ffc442c6aaf`

### 2. CLAIM-f3596def4af3193aff69

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：整体流程：APPROVED->CANCELLED 与 APPROVED->PAYING 互斥竞争，只能一方成功。 代码行为：取消成功后（->CANCELLED），process 仍可把 CANCELLED 转 PAYING 并付款，两个互斥转换先后均成功。 候选差异：互斥仅被单次转换保证，CANCELLED 仍可进入 PAYING，流程互斥被打破。

证据：`EVID-51609b4bfe69ba121858`, `EVID-5f39d07111eece3fc55a`, `EVID-9eb4bc225e362ad11572`, `EVID-814f97268d0e37ff8b3e`, `EVID-985e80d990b668f69d70`

### 3. CLAIM-0b155c33fcd318596fd3

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：FR-04：付款任务必须使用原子 claim_for_payment，仅当持久化状态仍为 APPROVED 才改 PAYING。 代码行为：process 未调用 repository.claim_for_payment（infrastructure.py:101-107，守卫仅 APPROVED），改用 transition 且预期集合含 CANCELLED。 候选差异：实现绕开了唯一带 APPROVED 守卫的入口，等价于放宽守卫。

证据：`EVID-f37d09a9029386a0aa63`, `EVID-d1e40fad066795d82bde`, `EVID-e4628c5e5a0eb7bc923d`, `EVID-f0a3796fd3e07c57baff`, `EVID-1ea18dffbcb4f538854a`

### 4. CLAIM-5385d34115e004dc98d6

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：FR-04：取消与付款抢占并发时只允许一个状态转换成功。 代码行为：cancel(APPROVED->CANCELLED) 与 process 均成功时，CANCELLED 仍被转 PAYING 并扣款，两个转换都成功。 候选差异：付款路径不再与取消互斥。

证据：`EVID-5043f2c131417a283dbd`, `EVID-3b0620931e73654e19bb`, `EVID-48148e01a54fca75ec58`, `EVID-4042a79076d8147cee40`, `EVID-0f2e7785b32f2825ddb3`

### 5. CLAIM-4bfcd15d2a888c7f839e

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：验收标准：已取消单据不调用付款网关。 代码行为：CANCELLED 满足 transition 预期集合后被置 PAYING，随后 gateway.pay 被调用。 候选差异：已取消单据会调用付款网关，直接违反验收标准。

证据：`EVID-19582711d0905a146525`, `EVID-756bcb0ae982c6fe0c6c`, `EVID-ff1ed99ede82fca86c66`, `EVID-5737dc039c42ce133972`, `EVID-8551c39f71ddc7743b25`

### 6. CLAIM-348fd8c162d3d5966bbf

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：验收标准：竞争测试最终状态只能是 CANCELLED 或从 PAYING 完成的付款结果，不得出现'状态是 CANCELLED 但网关已扣款'。 代码行为：顺序 cancel 成功后 process 仍 CANCELLED->PAYING->网关->PAID，可复现'CANCELLED 但已扣款'窗口。 候选差异：验收标准允许的终态集合被扩大。

证据：`EVID-0e934587bd0ff60f14e9`, `EVID-36133a03d415776a298d`, `EVID-a9b380104581d73b857a`, `EVID-afa1a78abff0a71fa27e`, `EVID-b4b293809fcf4e79f868`

### 7. CLAIM-c78fc576250c468bdb9e

- 判定：`inconsistent`
- 严重级别：`critical`
- 变更归因：`introduced`

文档期望：接口契约 §10.4：单据不是 APPROVED 时 process 返回 SKIPPED。 代码行为：CANCELLED 非 APPROVED 却转 PAYING 并付款；其余非 APPROVED 状态不在预期集合，transition 抛 StateConflictError 而非返回 SKIPPED。 候选差异：接口返回语义（SKIPPED）无法被满足。

证据：`EVID-54f933318095034096e9`, `EVID-ed0388ffcdd829de56ad`, `EVID-20e74234ac2e51a7d920`, `EVID-ff1b1a9a6bfcffd1d33d`, `EVID-cc3d8dc41ea3edc1abe4`

## 审查范围

```json
{
  "analysis_root": "/Users/8utterf1y/Desktop/agent项目/skills/02_03_SparkCheck/spec-review-opencode/examples/expense-review-benchmark",
  "base_revision": null,
  "documents": [
    "requirements/EXPENSE_PAYMENT_REQUIREMENTS.md"
  ],
  "full_repository": false,
  "head_revision": "0101b531adf6af94136bab1b6b7a9ec10718aef6",
  "note": "未提供基准版本；需求声明将通过可选的路径范围关联到代码。",
  "paths": [
    "versions/v5_cancelled_payment_bypass/expense_app"
  ],
  "review_type": "snapshot",
  "sections": [],
  "seed_count": 0
}
```
