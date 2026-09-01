from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any


class Role(str, Enum):
    EMPLOYEE = "EMPLOYEE"
    MANAGER = "MANAGER"
    FINANCE = "FINANCE"
    SYSTEM = "SYSTEM"


class ExpenseStatus(str, Enum):
    PENDING_MANAGER = "PENDING_MANAGER"
    PENDING_FINANCE = "PENDING_FINANCE"
    APPROVED = "APPROVED"
    PAYING = "PAYING"
    PAID = "PAID"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    PAYMENT_FAILED = "PAYMENT_FAILED"


class PaymentOutcome(str, Enum):
    SKIPPED = "SKIPPED"
    PAID = "PAID"
    FAILED = "FAILED"


@dataclass(frozen=True)
class Actor:
    user_id: str
    roles: frozenset[Role]


@dataclass
class Expense:
    id: str
    requester_id: str
    request_id: str
    amount: Decimal
    currency: str
    purpose: str
    status: ExpenseStatus = ExpenseStatus.PENDING_MANAGER
    payment_reference: str | None = None
    version: int = 0


@dataclass(frozen=True)
class AuditEvent:
    expense_id: str
    event_type: str
    actor_id: str
    from_status: ExpenseStatus | None
    to_status: ExpenseStatus
    occurred_at: datetime
    metadata: dict[str, Any] = field(default_factory=dict)


class ValidationError(ValueError):
    pass


class AuthorizationError(PermissionError):
    pass


class StateConflictError(RuntimeError):
    pass


class TransientPaymentError(RuntimeError):
    pass


class PermanentPaymentError(RuntimeError):
    pass
