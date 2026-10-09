"""Temporal contracts shared by the order saga and the services that run its
activities. Plumbing only — names and data shapes, no business logic (same
rule as sfo_events)."""

from sfo_contracts.errors import ACTIVITY_RETRY_POLICY, BusinessErrorType, business_error
from sfo_contracts.task_queues import TaskQueue
from sfo_contracts.workflows import (
    OrderSagaQuery,
    OrderSagaSignal,
    WorkflowName,
    order_workflow_id,
    refund_workflow_id,
)

__all__ = [
    "ACTIVITY_RETRY_POLICY",
    "BusinessErrorType",
    "business_error",
    "TaskQueue",
    "OrderSagaQuery",
    "OrderSagaSignal",
    "WorkflowName",
    "order_workflow_id",
    "refund_workflow_id",
]
