from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class RequestModel(BaseModel):
    """Base for every request body in every service (responses stay plain
    BaseModel).

    - extra="forbid": an unknown field is a 422, not silently dropped — a
      client typo ("quantiy") fails loudly instead of falling back to a
      default, and nothing outside the declared whitelist can ever reach a
      handler (mass-assignment defence, OWASP API3:2023).
    - str_strip_whitespace: "  " can't pass a min_length=1 check, and stored
      values don't carry stray padding. Fields where whitespace is
      significant (passwords) opt out with VerbatimStr.

    Every str and list field should also carry a max_length — unbounded input
    is a memory/storage cost an attacker controls (OWASP API4:2023).
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


# Opt-out of RequestModel's whitespace stripping, e.g. for passwords.
VerbatimStr = Annotated[str, StringConstraints(strip_whitespace=False)]

# Any client-supplied reference to another entity. Our IDs are 36-char
# UUIDv7 strings; the bound leaves room without accepting arbitrary blobs.
EntityId = Annotated[str, Field(min_length=1, max_length=64)]

# E.164: "+", country code, subscriber number — at most 15 digits in total.
PhoneNumber = Annotated[str, Field(pattern=r"^\+[1-9][0-9]{6,14}$", examples=["+923001234567"])]
