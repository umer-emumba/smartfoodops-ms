"""Ask the assistant from a shell, for trying it out before the streaming
API (step 3):

    docker compose exec ai-service python -m app.assistant --user <user-id> "spicy under 800"
    ... --conversation <id>     continue a conversation
    ... --restaurant <id>       as if on that restaurant's page
    ... --lat 33.72 --lng 73.05 the delivery location

Prints each event as it arrives; tokens inline."""

import argparse
import asyncio
import json

from app.assistant.deps import default_deps
from app.assistant.service import ChatRequest, run_turn
from app.db import SessionFactory


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("message")
    p.add_argument("--user", required=True)
    p.add_argument("--conversation")
    p.add_argument("--restaurant")
    p.add_argument("--lat", type=float)
    p.add_argument("--lng", type=float)
    a = p.parse_args()
    req = ChatRequest(
        user_id=a.user,
        message=a.message,
        conversation_id=a.conversation,
        restaurant_id=a.restaurant,
        lat=a.lat,
        lng=a.lng,
    )
    async for event in run_turn(req, default_deps(), SessionFactory):
        kind = event["type"]
        if kind == "token":
            print(event["text"], end="", flush=True)
        elif kind == "status":
            print(f"[{event['stage']}]", flush=True)
        elif kind == "cards":
            for c in event["dishes"]:
                print(
                    f"  • {c['quantity']} × {c['name']} — {c['restaurant_name']}, "
                    f"Rs {c['price']:,.0f}  {c['reason']}"
                )
            for r in event["restaurants"]:
                print(
                    f"  • {r['name']} ({r['cuisine']}) rating {r['avg_rating']}, "
                    f"dishes: {', '.join(r['dishes'])}"
                )
            for n in event["notes"]:
                print(f"  note: {n}")
        elif kind == "done":
            print("\n" + json.dumps(event))


if __name__ == "__main__":
    asyncio.run(main())
