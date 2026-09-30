from datetime import datetime

from pydantic import BaseModel


class RiderRegister(BaseModel):
    vehicle_type: str = "bike"  # bike | scooter | car
    license_plate: str | None = None


class RiderStatusUpdate(BaseModel):
    status: str  # available | busy | offline


class RiderLocationUpdate(BaseModel):
    lat: float
    lng: float


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
