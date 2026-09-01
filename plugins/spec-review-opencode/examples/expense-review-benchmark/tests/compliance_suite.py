#!/usr/bin/env python3
from __future__ import annotations

import argparse
import itertools
import json
import sys
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from pathlib import Path


parser = argparse.ArgumentParser()
parser.add_argument("version", type=Path)
args, remaining = parser.parse_known_args()
sys.path.insert(0, str(args.version.resolve()))

from expense_app import (  # noqa: E402
    Actor,
    AuthorizationError,
    CounterMetrics,
    ExpenseRepository,
    ExpenseService,
    ExpenseStatus,
    InMemoryAuditSink,
    PaymentOutcome,
    PayoutWorker,
    PermanentPaymentError,
    RecordingSleeper,
    Role,
    ScriptedPaymentGateway,
    TransientPaymentError,
    ValidationError,
)


EMPLOYEE = Actor("employee-1", frozenset({Role.EMPLOYEE}))
MANAGER = Actor("manager-1", frozenset({Role.MANAGER}))
FINANCE = Actor("finance-1", frozenset({Role.FINANCE}))


class ComplianceTest(unittest.TestCase):
    def make_app(self, barrier: threading.Barrier | None = None):
        counter = itertools.count(1)
        repository = ExpenseRepository(create_barrier=barrier)
        audit = InMemoryAuditSink()
        metrics = CounterMetrics()
        service = ExpenseService(
            repository, audit, metrics,
            id_factory=lambda: f"expense-{next(counter)}",
        )
        return repository, audit, metrics, service

    @staticmethod
    def submit(service: ExpenseService, request_id: str = "request-1", amount="100"):
        return service.submit(
            EMPLOYEE, request_id, Decimal(amount), "CNY", "客户现场交通费"
        )

    def approved(self, amount="100"):
        repository, audit, metrics, service = self.make_app()
        expense = self.submit(service, amount=amount)
        expense = service.approve_manager(MANAGER, expense.id)
        if expense.status == ExpenseStatus.PENDING_FINANCE:
            expense = service.approve_finance(FINANCE, expense.id)
        return repository, audit, metrics, service, expense

    def test_FR01_serial_duplicate_returns_same_expense(self):
        repository, _, _, service = self.make_app()
        first = self.submit(service)
        second = self.submit(service)
        self.assertEqual(first.id, second.id)
        self.assertEqual(repository.count_by_request(EMPLOYEE.user_id, "request-1"), 1)

    def test_FR01_concurrent_duplicate_is_atomic(self):
        workers = 8
        barrier = threading.Barrier(workers)
        repository, _, _, service = self.make_app(barrier)

        def create(_):
            return self.submit(service).id

        with ThreadPoolExecutor(max_workers=workers) as pool:
            ids = list(pool.map(create, range(workers)))
        self.assertEqual(len(set(ids)), 1)
        self.assertEqual(repository.count_by_request(EMPLOYEE.user_id, "request-1"), 1)

    def test_FR02_requester_cannot_approve_own_expense(self):
        _, _, _, service = self.make_app()
        expense = self.submit(service)
        self_approver = Actor(EMPLOYEE.user_id, frozenset({Role.EMPLOYEE, Role.MANAGER}))
        with self.assertRaises(AuthorizationError):
            service.approve_manager(self_approver, expense.id)

    def test_FR02_role_is_checked_server_side(self):
        _, _, _, service = self.make_app()
        expense = self.submit(service)
        with self.assertRaises(AuthorizationError):
            service.approve_manager(EMPLOYEE, expense.id)

    def test_FR03_threshold_boundaries_use_decimal(self):
        for amount, expected in [
            ("999.99", ExpenseStatus.APPROVED),
            ("1000.00", ExpenseStatus.APPROVED),
            ("1000.01", ExpenseStatus.PENDING_FINANCE),
        ]:
            _, _, _, service = self.make_app()
            expense = self.submit(service, amount=amount)
            approved = service.approve_manager(MANAGER, expense.id)
            self.assertEqual(approved.status, expected)

    def test_FR04_cancelled_expense_is_never_paid(self):
        repository, audit, metrics, service, expense = self.approved()
        cancelled = service.cancel(EMPLOYEE, expense.id)
        gateway = ScriptedPaymentGateway(["payment-ref"])
        worker = PayoutWorker(
            repository, audit, metrics, gateway, RecordingSleeper()
        )
        self.assertEqual(worker.process(cancelled.id), PaymentOutcome.SKIPPED)
        self.assertEqual(repository.get(cancelled.id).status, ExpenseStatus.CANCELLED)
        self.assertEqual(gateway.calls, [])

    def test_FR05_transient_retry_sequence(self):
        repository, audit, metrics, _, expense = self.approved()
        gateway = ScriptedPaymentGateway([
            TransientPaymentError("timeout"),
            TransientPaymentError("timeout"),
            "payment-ref",
        ])
        sleeper = RecordingSleeper()
        result = PayoutWorker(repository, audit, metrics, gateway, sleeper).process(expense.id)
        self.assertEqual(result, PaymentOutcome.PAID)
        self.assertEqual(len(gateway.calls), 3)
        self.assertEqual(sleeper.delays, [1.0, 2.0])
        self.assertTrue(all(key == expense.id for key, _ in gateway.calls))

    def test_FR05_permanent_failure_is_not_retried(self):
        repository, audit, metrics, _, expense = self.approved()
        gateway = ScriptedPaymentGateway([
            PermanentPaymentError("无效账户"),
            PermanentPaymentError("不应执行"),
            PermanentPaymentError("不应执行"),
            PermanentPaymentError("不应执行"),
        ])
        sleeper = RecordingSleeper()
        result = PayoutWorker(repository, audit, metrics, gateway, sleeper).process(expense.id)
        self.assertEqual(result, PaymentOutcome.FAILED)
        self.assertEqual(len(gateway.calls), 1)
        self.assertEqual(sleeper.delays, [])
        self.assertEqual(repository.get(expense.id).status, ExpenseStatus.PAYMENT_FAILED)

    def test_FR05_transient_attempt_cap_is_three(self):
        repository, audit, metrics, _, expense = self.approved()
        gateway = ScriptedPaymentGateway([
            TransientPaymentError("timeout") for _ in range(4)
        ])
        sleeper = RecordingSleeper()
        result = PayoutWorker(repository, audit, metrics, gateway, sleeper).process(expense.id)
        self.assertEqual(result, PaymentOutcome.FAILED)
        self.assertEqual(len(gateway.calls), 3)
        self.assertEqual(sleeper.delays, [1.0, 2.0])

    def test_FR06_audit_is_complete_and_excludes_purpose(self):
        repository, audit, metrics, service, expense = self.approved()
        gateway = ScriptedPaymentGateway(["payment-ref"])
        worker = PayoutWorker(
            repository, audit, metrics, gateway, RecordingSleeper()
        )
        self.assertEqual(worker.process(expense.id), PaymentOutcome.PAID)
        transitions = [(event.from_status, event.to_status) for event in audit.events]
        self.assertIn((None, ExpenseStatus.PENDING_MANAGER), transitions)
        self.assertIn((ExpenseStatus.PENDING_MANAGER, ExpenseStatus.APPROVED), transitions)
        self.assertIn((ExpenseStatus.APPROVED, ExpenseStatus.PAYING), transitions)
        self.assertIn((ExpenseStatus.PAYING, ExpenseStatus.PAID), transitions)
        serialized = json.dumps(
            [event.metadata for event in audit.events], ensure_ascii=False
        )
        self.assertNotIn(expense.purpose, serialized)
        self.assertGreater(metrics.values["payment_succeeded_total"], 0)

    def test_VALIDATION_rejects_invalid_submission(self):
        _, _, _, service = self.make_app()
        with self.assertRaises(ValidationError):
            service.submit(EMPLOYEE, "", Decimal("1"), "CNY", "用途")
        with self.assertRaises(ValidationError):
            service.submit(EMPLOYEE, "r", Decimal("0"), "CNY", "用途")
        with self.assertRaises(ValidationError):
            service.submit(EMPLOYEE, "r", Decimal("1"), "USD", "用途")


class RecordingResult(unittest.TextTestResult):
    def summary(self):
        bad = {test._testMethodName for test, _ in self.failures + self.errors}
        return {
            "version": args.version.name,
            "tests_run": self.testsRun,
            "passed": self.testsRun - len(bad),
            "failed_tests": sorted(bad),
        }


class RecordingRunner(unittest.TextTestRunner):
    resultclass = RecordingResult


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(ComplianceTest)
    result = RecordingRunner(verbosity=2).run(suite)
    print("BENCHMARK_RESULT=" + json.dumps(result.summary(), ensure_ascii=False))
    raise SystemExit(0 if result.wasSuccessful() else 1)
