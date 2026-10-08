import pytest
from app.schemas import AddressCreate, LoginRequest, RegisterRequest
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


def test_unknown_fields_rejected_not_ignored() -> None:
    with pytest.raises(ValidationError) as exc:
        RegisterRequest.model_validate({**_REG, "is_admin": True})
    assert exc.value.errors()[0]["type"] == "extra_forbidden"


def test_only_public_roles_can_self_register() -> None:
    assert RegisterRequest.model_validate({**_REG, "role": "rider"}).role == "rider"
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**_REG, "role": "system_admin"})


def test_phone_must_be_e164() -> None:
    for bad in ["03001234567", "+0300123", "+92 300 1234567", "+9230012345678901"]:
        with pytest.raises(ValidationError):
            RegisterRequest.model_validate({**_REG, "phone_number": bad})


def test_text_trimmed_but_password_kept_verbatim() -> None:
    req = RegisterRequest.model_validate(
        {**_REG, "full_name": "  Some One  ", "password": "  spaced pw  "}
    )
    assert req.full_name == "Some One"
    assert req.password == "  spaced pw  "
    assert LoginRequest(email="a@x.com", password=" pw ").password == " pw "
    with pytest.raises(ValidationError):
        RegisterRequest.model_validate({**_REG, "full_name": "   "})


def test_address_fields_bounded() -> None:
    base = {"line1": "1 St", "city": "Karachi", "lat": 1, "lng": 1}
    assert AddressCreate.model_validate(base).label == "Home"
    with pytest.raises(ValidationError):
        AddressCreate.model_validate({**base, "line1": "x" * 201})
