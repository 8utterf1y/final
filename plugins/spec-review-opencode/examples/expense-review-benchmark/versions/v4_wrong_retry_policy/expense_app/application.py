from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Callable
from uuid import uuid4

from .domain import (
    Actor,
    AuditEvent,
    AuthorizationError,
    Expense,
    ExpenseStatus,
    PaymentOutcome,
    PermanentPaymentError,
    Role,
    TransientPaymentError,
    ValidationError,
)
from .infrastructure import CounterMetrics, ExpenseRepository, InMemoryAuditSink


class ExpenseService:
    LARGE_EXPENSE_THRESHOLD = Decimal("1000.00")

    def __init__(
        self,
        repository: ExpenseRepository,
        audit: InMemoryAuditSink,
        metrics: CounterMetrics,
        id_factory: Callable[[], str] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.metrics = metrics
        self.id_factory = id_factory or (lambda: str(uuid4()))
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def submit(
        self,
        actor: Actor,
        request_id: str,
        amount: Decimal,
        currency: str,
        purpose: str,
    ) -> Expense:
        if Role.EMPLOYEE not in actor.roles:
            raise AuthorizationError("需要 EMPLOYEE 角色")
        self._validate_submission(request_id, amount, currency, purpose)

        def factory() -> Expense:
            return Expense(
                id=self.id_factory(), requester_id=actor.user_id,
                request_id=request_id, amount=amount, currency=currency,
                purpose=purpose,
            )

        expense, created = self.repository.create_or_get(
            actor.user_id, request_id, factory
        )
        if created:
            self._record("SUBMIT", actor.user_id, None, expense)
            self.metrics.increment("expense_submitted_total")
        return expense

    def approve_manager(self, actor: Actor, expense_id: str) -> Expense:
        expense = self.repository.get(expense_id)
        self._require_approver(actor, expense, Role.MANAGER)
        target = (
            ExpenseStatus.PENDING_FINANCE
            if expense.amount > self.LARGE_EXPENSE_THRESHOLD
            else ExpenseStatus.APPROVED
        )
        return self._transition(
            expense_id, {ExpenseStatus.PENDING_MANAGER}, target,
            "MANAGER_APPROVE", actor.user_id,
        )

    def approve_finance(self, actor: Actor, expense_id: str) -> Expense:
        expense = self.repository.get(expense_id)
        self._require_approver(actor, expense, Role.FINANCE)
        return self._transition(
            expense_id, {ExpenseStatus.PENDING_FINANCE}, ExpenseStatus.APPROVED,
            "FINANCE_APPROVE", actor.user_id,
        )

    def reject(self, actor: Actor, expense_id: str) -> Expense:
        expense = self.repository.get(expense_id)
        if expense.status == ExpenseStatus.PENDING_MANAGER:
            self._require_approver(actor, expense, Role.MANAGER)
        elif expense.status == ExpenseStatus.PENDING_FINANCE:
            self._require_approver(actor, expense, Role.FINANCE)
        else:
            from .domain import StateConflictError
            raise StateConflictError(f"不能在 {expense.status.value} 状态驳回")
        return self._transition(
            expense_id, {expense.status}, ExpenseStatus.REJECTED,
            "REJECT", actor.user_id,
        )

    def cancel(self, actor: Actor, expense_id: str) -> Expense:
        before, after = self.repository.cancel(expense_id, actor.user_id)
        self._record("CANCEL", actor.user_id, before.status, after)
        self.metrics.increment("expense_cancelled_total")
        return after

    def _require_approver(
        self, actor: Actor, expense: Expense, required_role: Role
    ) -> None:
        if required_role not in actor.roles:
            raise AuthorizationError(f"需要 {required_role.value} 角色")
        if actor.user_id == expense.requester_id:
            raise AuthorizationError("申请人不能审批或驳回自己的报销单")

    def _transition(
        self,
        expense_id: str,
        expected: set[ExpenseStatus],
        target: ExpenseStatus,
        event_type: str,
        actor_id: str,
        payment_reference: str | None = None,
    ) -> Expense:
        before, after = self.repository.transition(
            expense_id, expected, target, payment_reference
        )
        self._record(event_type, actor_id, before.status, after)
        self.metrics.increment("expense_transition_total")
        return after

    def _record(
        self,
        event_type: str,
        actor_id: str,
        from_status: ExpenseStatus | None,
        expense: Expense,
    ) -> None:
        self.audit.record(AuditEvent(
            expense_id=expense.id, event_type=event_type, actor_id=actor_id,
            from_status=from_status, to_status=expense.status,
            occurred_at=self.clock(), metadata={"version": expense.version},
        ))

    @staticmethod
    def _validate_submission(
        request_id: str, amount: Decimal, currency: str, purpose: str
    ) -> None:
        try:
            valid_amount = amount.is_finite() and amount > Decimal("0")
        except (AttributeError, InvalidOperation):
            valid_amount = False
        if not request_id.strip():
            raise ValidationError("request_id 不能为空")
        if not valid_amount:
            raise ValidationError("amount 必须是正数 Decimal")
        if currency != "CNY":
            raise ValidationError("仅支持 CNY")
        if not purpose.strip() or len(purpose) > 200:
            raise ValidationError("purpose 长度必须为 1 到 200 个字符")


class PayoutWorker:
    SYSTEM_ACTOR_ID = "payout-worker"

    def __init__(
        self,
        repository: ExpenseRepository,
        audit: InMemoryAuditSink,
        metrics: CounterMetrics,
        gateway: object,
        sleeper: object,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.audit = audit
        self.metrics = metrics
        self.gateway = gateway
        self.sleeper = sleeper
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def process(self, expense_id: str) -> PaymentOutcome:
        claimed = self.repository.claim_for_payment(expense_id)
        if claimed is None:
            return PaymentOutcome.SKIPPED
        before, paying = claimed
        self._record("PAYMENT_CLAIMED", before.status, paying)

        # 网关客户端对所有失败类型统一使用同一套处理策略。
        for attempt in range(1, 5):
            try:
                reference = self.gateway.pay(expense_id, paying)
                before, paid = self.repository.transition(
                    expense_id, {ExpenseStatus.PAYING}, ExpenseStatus.PAID,
                    payment_reference=reference,
                )
                self._record("PAYMENT_SUCCEEDED", before.status, paid)
                self.metrics.increment("payment_succeeded_total")
                return PaymentOutcome.PAID
            except (TransientPaymentError, PermanentPaymentError):
                self.metrics.increment("payment_retry_total")
                if attempt < 4:
                    self.sleeper.sleep(1.0)

        before, failed = self.repository.transition(
            expense_id, {ExpenseStatus.PAYING}, ExpenseStatus.PAYMENT_FAILED
        )
        self._record("PAYMENT_FAILED", before.status, failed)
        self.metrics.increment("payment_failed_total")
        return PaymentOutcome.FAILED

    def _record(
        self, event_type: str, from_status: ExpenseStatus, expense: Expense
    ) -> None:
        self.audit.record(AuditEvent(
            expense_id=expense.id, event_type=event_type,
            actor_id=self.SYSTEM_ACTOR_ID, from_status=from_status,
            to_status=expense.status, occurred_at=self.clock(),
            metadata={"version": expense.version},
        ))
