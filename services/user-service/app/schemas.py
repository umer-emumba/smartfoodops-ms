from datetime import datetime
from typing import Annotated, Literal, get_args

from pydantic import AfterValidator, BaseModel, EmailStr, Field
from sfo_common.schemas import PhoneNumber, RequestModel, VerbatimStr

# Public self-registration is limited to these roles — system_admin accounts
# are created out-of-band (app/create_admin.py), not self-serve. Anything else,
# including "system_admin", is a 422 from schema validation.
PublicRole = Literal["customer", "restaurant_admin", "rider"]
PUBLIC_ROLES: tuple[str, ...] = get_args(PublicRole)

# Lowercased so "A@x.com" and "a@x.com" can't become two accounts (the unique
# index on users.email is case-sensitive).
NormalizedEmail = Annotated[EmailStr, AfterValidator(str.lower)]
# Upper bound caps the cost of hashing attacker-supplied input with argon2.
# Verbatim: leading/trailing spaces are part of a password.
NewPassword = Annotated[VerbatimStr, Field(min_length=8, max_length=128)]
FullName = Annotated[str, Field(min_length=1, max_length=200)]


class RegisterRequest(RequestModel):
    email: NormalizedEmail
    password: NewPassword
    full_name: FullName
    phone_number: PhoneNumber
    role: PublicRole = "customer"


class NewAdmin(RequestModel):
    """The first system_admin's details, validated by the same rules as
    self-registration (app/create_admin.py)."""

    email: NormalizedEmail
    password: NewPassword
    full_name: FullName
    phone_number: PhoneNumber


class LoginRequest(RequestModel):
    email: NormalizedEmail
    # No min length here — login shouldn't reveal the password policy.
    password: Annotated[VerbatimStr, Field(max_length=128)]


class RefreshRequest(RequestModel):
    # "{uuid}.{43-char secret}" — see sfo_auth.tokens.generate_refresh_token.
    refresh_token: str = Field(max_length=256)


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


class AddressCreate(RequestModel):
    label: str = Field(default="Home", min_length=1, max_length=50)
    line1: str = Field(min_length=1, max_length=200)
    line2: str | None = Field(default=None, max_length=200)
    city: str = Field(min_length=1, max_length=100)
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
