from fastapi import APIRouter, Depends, Request, Response

from app.config import settings
from app.proxy import forward_request
from app.rate_limit import RateLimiter

# Registered before the generic proxy catch-all so these two specific paths
# get a stricter, separate rate limit — resists brute-force/credential-
# stuffing on login/register specifically. See docs/architecture.md#conventions.
router = APIRouter()

_auth_rate_limit = Depends(
    RateLimiter(times=settings.rate_limit_auth_per_minute, seconds=60, bucket="auth")
)


@router.post("/v1/auth/login", dependencies=[_auth_rate_limit])
async def login(request: Request) -> Response:
    return await forward_request("auth", "login", request)


@router.post("/v1/auth/register", dependencies=[_auth_rate_limit])
async def register(request: Request) -> Response:
    return await forward_request("auth", "register", request)
