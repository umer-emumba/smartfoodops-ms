from datetime import datetime
from typing import Annotated

from pydantic import AfterValidator, BaseModel, EmailStr, Field

# Public self-registration is limited to these roles — system_admin accounts
# are created out-of-band (seed script / DB), not self-serve.
PUBLIC_ROLES = ("customer", "restaurant_admin", "rider")

# Lowercased so "A@x.com" and "a@x.com" can't become two accounts (the unique
# index on users.email is case-sensitive).
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]
# Upper bound caps the cost of hashing attacker-supplied input with argon2.
NewPassword = Annotated[str, Field(min_length=8, max_length=128)]


class RegisterRequest(BaseModel):
    email: NormalizedEmail
    password: NewPassword
    full_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=5, max_length=20)
    role: str = "customer"


class BootstrapAdminRequest(BaseModel):
    email: NormalizedEmail
    password: NewPassword
    full_name: str = Field(min_length=1, max_length=200)
    phone_number: str = Field(min_length=5, max_length=20)


class LoginRequest(BaseModel):
    email: NormalizedEmail
    # No min length here — login shouldn't reveal the password policy.
    password: str = Field(max_length=128)


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
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
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
