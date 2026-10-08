from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field
from sfo_common.schemas import RequestModel


class RiderRegister(RequestModel):
    vehicle_type: Literal["bike", "scooter", "car"] = "bike"
    license_plate: str | None = Field(default=None, min_length=1, max_length=20)


class RiderStatusUpdate(RequestModel):
    status: Literal["available", "busy", "offline"]


class RiderLocationUpdate(RequestModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)


class RiderResponse(BaseModel):
    id: str
    user_id: str
    status: str
    verification_status: str
    vehicle_type: str
    license_plate: str | None
    last_lat: float | None
    last_lng: float | None
    created_at: datetime

    model_config = {"from_attributes": True}


class StatusChangeRequest(RequestModel):
    """Optional body for approve/suspend — recorded in the audit trail."""

    reason: str | None = Field(default=None, max_length=500)


class VerificationHistoryResponse(BaseModel):
    from_status: str | None
    to_status: str
    actor_id: str
    actor_role: str
    reason: str | None
    created_at: datetime

    model_config = {"from_attributes": True}
