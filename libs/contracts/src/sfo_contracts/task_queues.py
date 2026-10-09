from enum import StrEnum


class TaskQueue(StrEnum):
    """One task queue per service worker (docs/week2-plan.md D1): activities
    run in the service that owns the data they change. The saga workflows
    run on order-service's ORDER_SAGA queue and schedule activities onto the
    others."""

    ORDER_SAGA = "order-saga"
    ORDER_ACTIVITIES = "order-activities"
    RESTAURANT_ACTIVITIES = "restaurant-activities"
    PAYMENT_ACTIVITIES = "payment-activities"
    DELIVERY_ACTIVITIES = "delivery-activities"
