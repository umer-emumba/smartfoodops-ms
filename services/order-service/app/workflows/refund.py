"""OrderRefundWorkflow (docs/week2-plan.md D13): a system admin refunds a
delivered order, fully or partly. The order saga has finished by then, so
this is its own short workflow, ID `refund-{order_id}` — one per order
(plan §9).

    payment.refund (RefundProcessed event) → order.record_refund

The refund ID passed to payment-service is this workflow's ID, so a retried
refund activity can't refund twice. A business failure (e.g. more than was
captured) fails the workflow before anything is recorded; once the money is
refunded, recording it on the order is retried until it succeeds."""

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from sfo_contracts import ACTIVITY_RETRY_POLICY, TaskQueue, WorkflowName
    from sfo_contracts.activities import (
        OrderActivity,
        PaymentActivity,
        RecordRefundInput,
        RefundPaymentInput,
        RefundPaymentResult,
    )
    from sfo_contracts.workflows import OrderRefundInput

    from app.workflows.order_saga import ACTIVITY_TIMEOUT, MUST_SUCCEED


@workflow.defn(name=WorkflowName.ORDER_REFUND)
class OrderRefundWorkflow:
    @workflow.run
    async def run(self, data: OrderRefundInput) -> RefundPaymentResult:
        # Runs in payment-service: gives the money back and writes a
        # RefundProcessed event (the customer is notified).
        refund: RefundPaymentResult = await workflow.execute_activity(
            PaymentActivity.REFUND,
            RefundPaymentInput(
                order_id=data.order_id,
                refund_id=workflow.info().workflow_id,
                reason=data.reason,
                amount=data.amount,
            ),
            task_queue=TaskQueue.PAYMENT_ACTIVITIES,
            result_type=RefundPaymentResult,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=ACTIVITY_RETRY_POLICY,
        )
        # Runs in order-service: saves the refunded amount on the order.
        # Retried until it succeeds (the money has already gone back).
        await workflow.execute_activity(
            OrderActivity.RECORD_REFUND,
            RecordRefundInput(
                order_id=data.order_id,
                amount=refund.refunded_amount,
                reason=data.reason,
                actor=data.actor,
            ),
            task_queue=TaskQueue.ORDER_ACTIVITIES,
            start_to_close_timeout=ACTIVITY_TIMEOUT,
            retry_policy=MUST_SUCCEED,
        )
        return refund
