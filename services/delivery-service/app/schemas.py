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


class DropOutRequest(RequestModel):
    reason: str = Field(min_length=1, max_length=500)


class DeliveryResponse(BaseModel):
    id: str
    order_id: str
    status: str
    rider_id: str | None
    pickup_lat: float | None
    pickup_lng: float | None
    dropoff_lat: float | None
    dropoff_lng: float | None
    distance_km: float | None
    ready_at: datetime | None
    food_ready_at: datetime | None
    offer_expires_at: datetime | None
    pickup_eta: datetime | None
    dropoff_eta: datetime | None
    assigned_at: datetime | None
    picked_up_at: datetime | None
    delivered_at: datetime | None
    cancelled_at: datetime | None
    reassignment_count: int
    created_at: datetime

    model_config = {"from_attributes": True}


class OfferResponse(BaseModel):
    """What a rider sees before accepting: where to pick up and drop off."""

    delivery_id: str
    order_id: str
    pickup_lat: float
    pickup_lng: float
    dropoff_lat: float
    dropoff_lng: float
    delivery_distance_km: float  # restaurant → customer
    distance_to_pickup_km: float  # rider → restaurant, when offered
    expires_at: datetime
