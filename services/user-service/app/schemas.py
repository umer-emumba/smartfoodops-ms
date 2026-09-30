from datetime import datetime

from pydantic import BaseModel, EmailStr

# Public self-registration is limited to these roles — system_admin accounts
# are created out-of-band (seed script / DB), not self-serve.
PUBLIC_ROLES = ("customer", "restaurant_admin", "rider")


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone_number: str
    role: str = "customer"


class BootstrapAdminRequest(BaseModel):
    email: EmailStr
    password: str
    full_name: str
    phone_number: str


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"


class UserResponse(BaseModel):
    id: str
    email: str
    full_name: str
    phone_number: str
    role: str
    email_verified: bool
    phone_verified: bool
    is_blocked: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class AddressCreate(BaseModel):
    label: str = "Home"
    line1: str
    line2: str | None = None
    city: str
    lat: float
    lng: float
    is_default: bool = False


class AddressResponse(BaseModel):
    id: str
    label: str
    line1: str
    line2: str | None
    city: str
    lat: float
    lng: float
    is_default: bool
    created_at: datetime

    model_config = {"from_attributes": True}
