import pytest
from app.schemas import LoginRequest, RegisterRequest
from pydantic import ValidationError

_REG = {
    "email": "Someone@Example.COM",
    "password": "long-enough-pw",
    "full_name": "Some One",
    "phone_number": "+923001234567",
}


def test_email_normalized_to_lowercase() -> None:
    assert RegisterRequest.model_validate(_REG).email == "someone@example.com"
    assert LoginRequest(email="A@Example.com", password="x").email == "a@example.com"


def test_password_length_enforced_on_registration() -> None:
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**_REG, "password": "short"})
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**_REG, "password": "x" * 129})
