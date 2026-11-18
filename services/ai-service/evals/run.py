"""Assistant eval (docs/week5-plan.md §4, step 2): the golden questions in
evals/assistant_golden.json through the real graph — the configured LLM,
the live index and restaurant-service — checked in code. No LLM judge.

    docker compose exec ai-service python -m evals.run            # report
    docker compose exec ai-service python -m evals.run --check    # + fail under the bar
    ... --only negation,budget      cases with any of these tags
    ... --json /tmp/out.json        per-case results, for comparing runs

Each run calls the LLM 2–3 times per case: with Claude about a cent a case
(the cost is measured and printed), with a local model free but slow.

What is checked:
- intent: the route the understand step chose.
- filters: what the customer asked for (expect.filters) against what was
  extracted — spice, diet, exclusions, cuisine, budget in PKR, people,
  "this restaurant", rating, follow-up.
- cards: some / none as expected (none = said "not on the menu").
- violations (a gate: must be 0): a card that isn't in the index under that
  ID, name and restaurant; or one that breaks a constraint the customer
  stated — spice, diet, an excluded word, cuisine, the page's restaurant, a
  meal over budget or split across restaurants; or a prompt leak.
- relevant: at least one card is a known-good answer (reported, not gated).
- latency, tokens and cost per case.
"""

import argparse
import asyncio
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from app import qdrant
from app.assistant.deps import default_deps
from app.assistant.graph import Deps, graph
from app.assistant.service import _meta
from app.documents import words
from app.llm import usage_scope
from app.store import ORDER_HISTORY, RESTAURANTS, name
from qdrant_client.http import models as m

GOLDEN = Path(__file__).with_name("assistant_golden.json")
MIN_INTENT = 0.90
MIN_FILTERS = 0.85
MIN_CARDS = 0.85
LEAK_MARKERS = ("Untrusted text", "You are the food assistant", "How to answer:")


async def _demo_customer() -> str:
    """The customer with the most delivered orders in the index."""
    points, _ = await qdrant.client.scroll(
        name(ORDER_HISTORY),
        scroll_filter=m.Filter(
            must=[m.FieldCondition(key="doc_type", match=m.MatchValue(value="customer_summary"))]
        ),
        limit=1000,
        with_payload=["customer_id", "order_count"],
    )
    best = max(points, key=lambda p: (p.payload or {}).get("order_count", 0))
    return str(best.payload["customer_id"])  # type: ignore[index]


async def _restaurant_ids() -> dict[str, str]:
    points, _ = await qdrant.client.scroll(
        name(RESTAURANTS), limit=1000, with_payload=["name", "restaurant_id"]
    )
    return {p.payload["name"]: p.payload["restaurant_id"] for p in points if p.payload}


async def _turn(deps: Deps, state: Any) -> tuple[dict[str, Any], str]:
    final: dict[str, Any] = {}
    text: list[str] = []
    async for mode, chunk in graph.astream(state, context=deps, stream_mode=["custom", "values"]):
        if mode == "values":
            final = chunk  # type: ignore[assignment]
        elif chunk.get("type") == "token":  # type: ignore[union-attr]
            text.append(chunk["text"])  # type: ignore[index]
    return final, "".join(text)


def _filter_checks(expect: dict[str, Any], final: dict[str, Any]) -> list[str]:
    """What the understand step got wrong, as readable misses."""
    u = final.get("understanding")
    if u is None:
        return ["nothing understood"]
    c, miss = u.constraints, []
    for key, want in expect.items():
        if key == "max_spice_at_most" and (c.max_spice is None or c.max_spice > want):
            miss.append(f"max_spice {c.max_spice}, want ≤ {want}")
        elif key == "dietary" and not set(want) <= set(c.dietary):
            miss.append(f"dietary {c.dietary}, want {want}")
        elif key == "nut_free_or_exclude_nut" and not (
            "nut_free" in c.dietary or {"nut", "nuts"} & set(c.exclude)
        ):
            miss.append(f"no nut filter (dietary {c.dietary}, exclude {c.exclude})")
        elif key == "exclude_any" and not set(words(*want)) & set(words(*c.exclude)):
            miss.append(f"exclude {c.exclude}, want one of {want}")
        elif key == "cuisines_any" and not set(words(*want)) & set(words(*c.cuisines)):
            miss.append(f"cuisines {c.cuisines}, want one of {want}")
        elif key == "budget_pkr" and not _close(final.get("budget_pkr"), want):
            miss.append(f"budget Rs {final.get('budget_pkr')}, want Rs {want}")
        elif key == "price_cap_pkr":
            caps = [p for p in (final.get("budget_pkr"), final.get("max_each_pkr")) if p]
            if not caps or not _close(min(caps), want):
                miss.append(f"price cap {caps}, want Rs {want}")
        elif key == "people" and c.people != want:
            miss.append(f"people {c.people}, want {want}")
        elif key == "this_restaurant" and u.this_restaurant != want:
            miss.append(f"this_restaurant {u.this_restaurant}")
        elif key == "min_rating_at_least" and (c.min_rating is None or c.min_rating < want):
            miss.append(f"min_rating {c.min_rating}, want ≥ {want}")
        elif key == "refers_to_previous" and u.refers_to_previous != want:
            miss.append(f"refers_to_previous {u.refers_to_previous}")
    return miss


def _close(got: float | None, want: float) -> bool:
    return got is not None and abs(got - want) <= 0.02 * want


async def _violations(
    case: dict[str, Any], final: dict[str, Any], text: str, deps: Deps, page_id: str | None
) -> list[str]:
    expect = case["expect"]
    filters = expect.get("filters", {})
    dishes = final.get("dishes", [])
    wrong: list[str] = []
    indexed = {h.id: h.data for h in await deps.retriever.items_by_id([c.item_id for c in dishes])}
    for card in dishes:
        data = indexed.get(card.item_id)
        if data is None:
            wrong.append(f"{card.name}: not an orderable dish in the index")
            continue
        if (data.get("name"), data.get("restaurant_id")) != (card.name, card.restaurant_id):
            wrong.append(f"{card.name}: name/restaurant don't match the index")
        if "max_spice_at_most" in filters and data["spice_level"] > filters["max_spice_at_most"]:
            wrong.append(f"{card.name}: spice {data['spice_level']}")
        if missing := set(filters.get("dietary", [])) - set(data.get("dietary_tags") or []):
            wrong.append(f"{card.name}: not {', '.join(missing)}")
        text_words = set(words(data.get("name"), data.get("category"), data.get("description")))
        if banned := set(words(*filters.get("exclude_any", []))) & text_words:
            wrong.append(f"{card.name}: mentions {', '.join(banned)}")
        if (
            filters.get("nut_free_or_exclude_nut")
            and "nut_free" not in (data.get("dietary_tags") or [])
            and {"nut", "almond", "pistachio", "walnut", "peanut", "cashew"} & text_words
        ):
            wrong.append(f"{card.name}: has nuts")
        cuisines = set(words(*filters.get("cuisines_any", [])))
        if cuisines and not cuisines & set(words(data.get("cuisine"))):
            wrong.append(f"{card.name}: cuisine {data.get('cuisine')}")
        if filters.get("this_restaurant") and card.restaurant_id != page_id:
            wrong.append(f"{card.name}: not from the page's restaurant")
    if final.get("meal") and dishes:
        total = sum(c.price * c.quantity for c in dishes)
        if (budget := filters.get("budget_pkr")) and total > budget * 1.001:
            wrong.append(f"meal total Rs {total:.0f} over Rs {budget}")
        if len({c.restaurant_id for c in dishes}) > 1:
            wrong.append("meal from more than one restaurant")
    if expect.get("no_leak") and any(marker in text for marker in LEAK_MARKERS):
        wrong.append("the answer quotes the system prompt")
    return wrong


def _relevant(expect: dict[str, Any], final: dict[str, Any]) -> bool | None:
    want = expect.get("relevant_any")
    if not want:
        return None
    shown = {f"{c.restaurant_name} / {c.name}" for c in final.get("dishes", [])}
    shown |= {r.name for r in final.get("restaurants", [])}
    return bool(shown & set(want))


async def run_case(
    case: dict[str, Any], deps: Deps, user_id: str, restaurants: dict[str, str]
) -> dict[str, Any]:
    page_id = restaurants.get(case["page"]) if case.get("page") else None
    history: list[dict[str, str]] = []
    previous: dict[str, Any] | None = None
    base = {
        "user_id": user_id,
        "summary": "",
        "page_restaurant_id": page_id,
        "lat": case.get("lat"),
        "lng": case.get("lng"),
    }
    for earlier in case.get("before", []):
        final, text = await _turn(
            deps, {**base, "message": earlier, "history": history, "previous": previous}
        )
        history += [{"role": "user", "content": earlier}, {"role": "assistant", "content": text}]
        previous = _meta(final)  # type: ignore[arg-type]
    started = time.perf_counter()
    with usage_scope() as usage:
        final, text = await _turn(
            deps, {**base, "message": case["q"], "history": history, "previous": previous}
        )
    seconds = time.perf_counter() - started
    expect = case["expect"]
    u = final.get("understanding")
    has_cards = bool(final.get("dishes") or final.get("restaurants"))
    cards_ok = {"some": has_cards, "none": not has_cards, "any": True}[expect["cards"]]
    if expect["cards"] == "some" and expect.get("kind") == "restaurants":
        cards_ok = bool(final.get("restaurants"))
    spice_min = expect.get("card_spice_at_least")
    return {
        "q": case["q"],
        "tags": case.get("tags", []),
        "intent": u.intent if u else None,
        "intent_ok": u is not None and u.intent == expect["intent"],
        "filter_misses": _filter_checks(expect.get("filters", {}), final),
        "has_filters": bool(expect.get("filters")),
        "cards_ok": cards_ok,
        "violations": await _violations(case, final, text, deps, page_id),
        "relevant": _relevant(expect, final),
        "spicy_enough": None
        if spice_min is None or not final.get("dishes")
        else sum((c.spice_level or 0) >= spice_min for c in final["dishes"])
        >= len(final["dishes"]) / 2,
        "status": final.get("status"),
        "degraded_reason": final.get("degraded_reason"),
        "made_up_caught": final.get("made_up", 0),
        "shown": [
            f"{c.quantity}× {c.restaurant_name} / {c.name} Rs {c.price:.0f}"
            for c in final.get("dishes", [])
        ]
        + [r.name for r in final.get("restaurants", [])],
        "text": text,
        "seconds": round(seconds, 2),
        "llm_calls": usage.calls,
        "input_tokens": usage.input_tokens,
        "output_tokens": usage.output_tokens,
        "cost_usd": round(usage.cost_usd, 6),
        "steps": usage.steps,
    }


def _per_step(results: list[dict[str, Any]]) -> dict[str, dict[str, float]]:
    """Average tokens and time per case for each step (over all cases, so a
    step that some turns skip counts 0 for those)."""
    totals: dict[str, dict[str, int]] = {}
    for r in results:
        for step, s in r["steps"].items():
            t = totals.setdefault(step, {})
            for k, v in s.items():
                t[k] = t.get(k, 0) + v
    return {
        step: {k: round(v / len(results), 1) for k, v in t.items()}
        for step, t in sorted(totals.items())
    }


def _share(results: list[dict[str, Any]], key: str) -> float:
    return sum(bool(r[key]) for r in results) / len(results) if results else 1.0


async def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--check", action="store_true", help="exit 1 under the bar")
    p.add_argument("--only", help="comma-separated tags")
    p.add_argument("--json", type=Path)
    p.add_argument("--user", help="customer ID (default: the one with most orders)")
    args = p.parse_args()

    cases = json.loads(GOLDEN.read_text())["cases"]
    if args.only:
        tags = set(args.only.split(","))
        cases = [c for c in cases if tags & set(c.get("tags", []))]
    deps = default_deps()
    if not deps.llm.configured:
        sys.exit("No LLM configured (LLM_API_KEY / LLM_BASE_URL): the eval needs a model.")
    user_id = args.user or await _demo_customer()
    restaurants = await _restaurant_ids()
    print(
        f"{len(cases)} cases · provider {deps.llm.provider} · "
        f"smart {deps.llm.model_name('smart')} · fast {deps.llm.model_name('fast')}\n"
    )

    results = []
    for case in cases:
        r = await run_case(case, deps, user_id, restaurants)
        results.append(r)
        flags = []
        if not r["intent_ok"]:
            flags.append(f"intent={r['intent']}")
        if r["filter_misses"]:
            flags.append("filters: " + "; ".join(r["filter_misses"]))
        if not r["cards_ok"]:
            flags.append(f"cards={'some' if r['shown'] else 'none'}")
        if r["violations"]:
            flags.append("VIOLATION: " + "; ".join(r["violations"]))
        if r["relevant"] is False:
            flags.append("not relevant")
        if r["degraded_reason"]:
            flags.append(f"degraded={r['degraded_reason']}")
        mark = "ok " if not flags else "-- "
        print(f"{mark}{r['seconds']:5.1f}s {r['q'][:60]!r}")
        for s in r["shown"][:4]:
            print(f"        {s}")
        if flags:
            print("        " + " | ".join(flags))

    with_filters = [r for r in results if r["has_filters"]]
    relevant = [r for r in results if r["relevant"] is not None]
    seconds = [r["seconds"] for r in results]
    violations = sum(len(r["violations"]) for r in results)
    summary = {
        "cases": len(results),
        "intent": round(_share(results, "intent_ok"), 3),
        "filters": round(
            sum(not r["filter_misses"] for r in with_filters) / max(len(with_filters), 1), 3
        ),
        "cards": round(_share(results, "cards_ok"), 3),
        "relevant": round(_share(relevant, "relevant"), 3),
        "violations": violations,
        "made_up_caught": sum(r["made_up_caught"] for r in results),
        "degraded": sum(bool(r["degraded_reason"]) for r in results),
        "seconds_p50": round(statistics.median(seconds), 2),
        "seconds_p95": round(sorted(seconds)[max(int(len(seconds) * 0.95) - 1, 0)], 2),
        "llm_calls": sum(r["llm_calls"] for r in results),
        "cost_usd_total": round(sum(r["cost_usd"] for r in results), 4),
        "cost_usd_per_case": round(sum(r["cost_usd"] for r in results) / len(results), 5),
        "tokens_per_case": round(
            sum(r["input_tokens"] + r["output_tokens"] for r in results) / len(results)
        ),
    }
    print()
    for k, v in summary.items():
        print(f"{k:>18}: {v}")
    per_step = _per_step(results)
    print("\nper case, by step: calls · input (cached) · output (reasoning) · seconds")
    for step, a in per_step.items():
        print(
            f"{step:>18}: {a['calls']:.1f} · {a['input']:.0f} ({a['cached']:.0f}) · "
            f"{a['output']:.0f} ({a['reasoning']:.0f}) · {a['ms'] / 1000:.1f}"
        )
    summary["per_step"] = per_step  # type: ignore[assignment]
    if args.json:
        args.json.write_text(json.dumps({"summary": summary, "results": results}, indent=1))
    if args.check:
        failed = [
            f"intent {summary['intent']} < {MIN_INTENT}" if summary["intent"] < MIN_INTENT else "",
            f"filters {summary['filters']} < {MIN_FILTERS}"
            if summary["filters"] < MIN_FILTERS
            else "",
            f"cards {summary['cards']} < {MIN_CARDS}" if summary["cards"] < MIN_CARDS else "",
            f"{violations} violation(s)" if violations else "",
        ]
        if any(failed):
            sys.exit("Below the bar: " + ", ".join(f for f in failed if f))


if __name__ == "__main__":
    asyncio.run(main())
