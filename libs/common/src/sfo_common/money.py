from decimal import Decimal
from typing import Annotated

from pydantic import PlainSerializer

# Money in API responses. Decimal everywhere in Python — it's what Numeric(10,2)
# columns return and what totals are computed with — but emitted as a JSON
# number, which is the API contract clients already rely on. Safe at this
# scale: a value with at most 10 digits and 2 decimals converts to the float
# whose shortest repr is the same decimal text (8.5, 0.1, 12345678.99).
MoneyAmount = Annotated[Decimal, PlainSerializer(float, return_type=float, when_used="json")]
