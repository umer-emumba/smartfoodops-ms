from datetime import timedelta
from enum import StrEnum

from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError


class BusinessErrorType(StrEnum):
    """`ApplicationError.type` for business failures. Retrying one can't
    change the outcome, so they're raised non-retryable and the saga
    compensates instead."""

    RESTAURANT_UNAVAILABLE = "RestaurantUnavailable"
    CAPACITY_FULL = "CapacityFull"
    OUT_OF_STOCK = "OutOfStock"
    PROMOTION_INVALID = "PromotionInvalid"
    PAYMENT_DECLINED = "PaymentDeclined"
    INVALID_PAYMENT_STATE = "InvalidPaymentState"
    REFUND_EXCEEDS_CAPTURED = "RefundExceedsCaptured"
    INVALID_ORDER_TRANSITION = "InvalidOrderTransition"
    NOT_FOUND = "NotFound"


def business_error(error_type: BusinessErrorType, message: str) -> ApplicationError:
    """Build the error an activity raises for a business failure:
    `raise business_error(BusinessErrorType.OUT_OF_STOCK, "...")`."""
    return ApplicationError(message, type=error_type.value, non_retryable=True)


# Every other exception is treated as an infrastructure error (DB/Redis
# blip, timeout, worker crash) and retried with exponential backoff:
# 1s, 2s, 4s, 8s — 5 attempts in total. The business error types are listed
# here too, so a business error raised without non_retryable=True still
# isn't retried.
ACTIVITY_RETRY_POLICY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=5,
    non_retryable_error_types=[t.value for t in BusinessErrorType],
)
