from __future__ import annotations

from collections import Counter
from dataclasses import replace
from threading import Barrier, RLock
from typing import Callable, Iterable

from .domain import AuditEvent, Expense, ExpenseStatus, StateConflictError


class ExpenseRepository:
    """基准应用使用的内存事务边界。"""

    def __init__(self, create_barrier: Barrier | None = None) -> None:
        self._lock = RLock()
        self._by_id: dict[str, Expense] = {}
        self._by_request: dict[tuple[str, str], str] = {}
        self._create_barrier = create_barrier

    @staticmethod
    def _copy(expense: Expense) -> Expense:
        return replace(expense)

    def create_or_get(
        self,
        requester_id: str,
        request_id: str,
        factory: Callable[[], Expense],
    ) -> tuple[Expense, bool]:
        key = (requester_id, request_id)
        with self._lock:
            existing_id = self._by_request.get(key)
            if existing_id is not None:
                return self._copy(self._by_id[existing_id]), False
            expense = factory()
            self._by_id[expense.id] = self._copy(expense)
            self._by_request[key] = expense.id
            return self._copy(expense), True

    def find_by_request(self, requester_id: str, request_id: str) -> Expense | None:
        with self._lock:
            expense_id = self._by_request.get((requester_id, request_id))
            return self._copy(self._by_id[expense_id]) if expense_id else None

    def create_unchecked(self, expense: Expense) -> Expense:
        """便于测试的底层插入接口；调用方必须自行保证事务隔离。"""
        if self._create_barrier is not None:
            self._create_barrier.wait(timeout=5)
        with self._lock:
            self._by_id[expense.id] = self._copy(expense)
            self._by_request[(expense.requester_id, expense.request_id)] = expense.id
            return self._copy(expense)

    def get(self, expense_id: str) -> Expense:
        with self._lock:
            return self._copy(self._by_id[expense_id])

    def count_by_request(self, requester_id: str, request_id: str) -> int:
        with self._lock:
            return sum(
                1 for item in self._by_id.values()
                if item.requester_id == requester_id and item.request_id == request_id
            )

    def transition(
        self,
        expense_id: str,
        expected: set[ExpenseStatus],
        target: ExpenseStatus,
        payment_reference: str | None = None,
    ) -> tuple[Expense, Expense]:
        with self._lock:
            current = self._by_id[expense_id]
            if current.status not in expected:
                raise StateConflictError(
                    f"不能从 {current.status.value} 转换到 {target.value}"
                )
            before = self._copy(current)
            current.status = target
            current.version += 1
            if payment_reference is not None:
                current.payment_reference = payment_reference
            return before, self._copy(current)

    def cancel(self, expense_id: str, requester_id: str) -> tuple[Expense, Expense]:
        with self._lock:
            current = self._by_id[expense_id]
            if current.requester_id != requester_id:
                raise StateConflictError("只有申请人可以取消报销单")
            if current.status not in {
                ExpenseStatus.PENDING_MANAGER,
                ExpenseStatus.PENDING_FINANCE,
                ExpenseStatus.APPROVED,
            }:
                raise StateConflictError(f"不能在 {current.status.value} 状态取消")
            before = self._copy(current)
            current.status = ExpenseStatus.CANCELLED
            current.version += 1
            return before, self._copy(current)

    def claim_for_payment(self, expense_id: str) -> tuple[Expense, Expense] | None:
        try:
            return self.transition(
                expense_id, {ExpenseStatus.APPROVED}, ExpenseStatus.PAYING
            )
        except StateConflictError:
            return None


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []
        self._lock = RLock()

    def record(self, event: AuditEvent) -> None:
        with self._lock:
            self.events.append(event)


class CounterMetrics:
    def __init__(self) -> None:
        self.values: Counter[str] = Counter()

    def increment(self, name: str) -> None:
        self.values[name] += 1


class RecordingSleeper:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def sleep(self, seconds: float) -> None:
        self.delays.append(seconds)


class ScriptedPaymentGateway:
    """每个预设结果可以是付款流水号，也可以是需要抛出的异常。"""

    def __init__(self, outcomes: Iterable[str | Exception]) -> None:
        self._outcomes = iter(outcomes)
        self.calls: list[tuple[str, str]] = []

    def pay(self, idempotency_key: str, expense: Expense) -> str:
        self.calls.append((idempotency_key, expense.id))
        outcome = next(self._outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome
