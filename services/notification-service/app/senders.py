"""Delivery per channel (docs/week3-plan.md W3-D7). In-app needs nothing —
the row is the notification. Email and SMS are simulated: no provider, a
log line instead, and a configurable share of sends fail so the retry path
is exercised. A real sender would look up the address/number from a
user-contact projection (user-service events), keeping this service free of
calls to other services."""

import logging
import random

from app.config import settings
from app.models import Notification

logger = logging.getLogger(__name__)


class SendError(Exception):
    """The provider refused or failed — worth retrying."""


async def send(notification: Notification) -> None:
    if notification.channel == "in_app":
        return
    if random.random() < settings.notify_fake_failure_rate:
        raise SendError(f"simulated {notification.channel} provider failure")
    logger.info(
        "Sent %s notification %s to user %s (%s)",
        notification.channel,
        notification.id,
        notification.user_id,
        notification.type,
    )
