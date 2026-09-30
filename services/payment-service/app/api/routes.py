from fastapi import APIRouter, Depends
from sfo_auth import CurrentUser, Role
from sfo_common.errors import ForbiddenError, NotFoundError
from sqlalchemy.ext.asyncio import AsyncSession

from app import repository
from app.auth_deps import get_current_user
from app.db import get_session
from app.models import Payment
from app.schemas import PaymentResponse

router = APIRouter(prefix="/v1", tags=["payments"])

# Week 1: read-only skeleton. authorize/capture/refund (with idempotency-key
# handling) is driven by the order saga starting Week 2 — see docs/PROGRESS.md.


@router.get("/payments/by-order/{order_id}", response_model=PaymentResponse)
async def get_payment_for_order(
    order_id: str,
    current: CurrentUser = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> Payment:
    payment = await repository.get_payment_by_order_id(session, order_id)
    if payment is None:
        raise NotFoundError("No payment recorded for this order")
    if payment.customer_id != current.id and current.role != Role.SYSTEM_ADMIN:
        raise ForbiddenError("Not your payment")
    return payment
