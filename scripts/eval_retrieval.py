"""Retrieval eval (docs/week4-plan.md step 5, B4-D17): runs the golden queries
in scripts/fixtures/retrieval_golden.json against the search API and reports
recall@5 and MRR, per query, per kind, per tag, per split and overall.

    uv run --group dev python scripts/eval_retrieval.py              # report
    uv run --group dev python scripts/eval_retrieval.py --check      # + fail under the bar
    uv run --group dev python scripts/eval_retrieval.py --split test --json out.json

Each query has a split — dev (the original 32, which the fusion weights were
tuned on) or test (held out, for comparing models and settings fairly) — and
tags for what it exercises (exact, vague, attribute, roman_urdu, typo,
negation, multi), so a change that helps one kind of query and hurts another
shows up instead of averaging out.

Needs the seeded stack with the index built (`docker compose up`,
scripts/seed.py, `docker compose exec ai-service python -m app.reindex`).

- recall@5: of the relevant answers, the share in the top 5 — out of at
  most 5, so a query with 20 right answers isn't capped at 25%.
- MRR: 1 / the rank of the first relevant hit in the top 10 (0 if none),
  averaged — how high the first good answer comes.

Item names are matched as "Restaurant / Item", so the golden set doesn't
depend on database IDs; every name is checked against the catalog fixture
first, so a typo fails loudly instead of quietly lowering the score.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOP_K = 5
MRR_DEPTH = 10
# The bar `--check` (scripts/check.sh --with-ai) holds the index to, over all
# 106 queries: the measured baseline (recall@5 0.748, MRR 0.843 on 2026-11-11,
# identical across runs) less ~0.05, so a regression of a few queries fails
# but noise doesn't. The test split is deliberately hard (Roman Urdu,
# negation, typos), so the bar is lower than the old 32-query one.
MIN_RECALL = 0.70
MIN_MRR = 0.79


class _WaitOutRateLimit(httpx.HTTPTransport):
    """The gateway allows 120 requests/min per IP; one eval is ~100, so runs
    back to back (or after the smoke test) hit it — wait, don't fail."""

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        while True:
            resp = super().handle_request(request)
            if resp.status_code != 429:
                return resp
            resp.read()
            wait_ms = resp.json()["error"]["details"].get("retry_after_ms", 5_000)
            time.sleep(wait_ms / 1000 + 0.1)


def _catalog_names() -> tuple[set[str], set[str], set[str]]:
    catalog = json.loads((FIXTURES / "catalog.json").read_text())
    restaurants = {r["name"] for r in catalog["restaurants"]}
    cuisines = {r["cuisine_type"] for r in catalog["restaurants"]}
    items = {
        f"{r['name']} / {i['name']}"
        for r in catalog["restaurants"]
        for c in r["menu"]
        for i in c["items"]
    }
    return restaurants, cuisines, items


def _check_golden(queries: list[dict[str, Any]]) -> None:
    restaurants, cuisines, items = _catalog_names()
    known = {"item": items, "restaurant": restaurants, "cuisine": cuisines}
    for q in queries:
        for rel in q["relevant"]:
            if q["kind"] == "review":
                ok = rel["restaurant"] is None or rel["restaurant"] in restaurants
            else:
                ok = rel in known[q["kind"]]
            if not ok:
                sys.exit(f"Golden set: {rel!r} ({q['q']!r}) is not in the catalog fixture")


def _label(kind: str, hit: dict[str, Any]) -> str:
    data = hit["data"]
    if kind == "item":
        return f"{data['restaurant_name']} / {data['name']}"
    if kind == "restaurant":
        return str(data["name"])
    if kind == "review":
        return f"{data['restaurant_name']}: {hit['text'][:60]}"
    return str(hit["id"])


def _is_relevant(kind: str, hit: dict[str, Any], relevant: list[Any]) -> bool:
    if kind == "review":
        text = hit["text"].lower()
        return any(
            (r["restaurant"] is None or r["restaurant"] == hit["data"]["restaurant_name"])
            and any(word in text for word in r["any"])
            for r in relevant
        )
    return _label(kind, hit) in relevant


def evaluate(client: httpx.Client, q: dict[str, Any]) -> dict[str, Any]:
    params = {"q": q["q"], "kind": q["kind"], "limit": MRR_DEPTH, **q["params"]}
    resp = client.get("/v1/ai/search", params=params)
    resp.raise_for_status()
    hits = resp.json()["hits"]
    marks = [_is_relevant(q["kind"], h, q["relevant"]) for h in hits]
    # Reviews: any matching review counts, so the ideal is a full top 5.
    wanted = TOP_K if q["kind"] == "review" else min(len(q["relevant"]), TOP_K)
    recall = min(sum(marks[:TOP_K]) / wanted, 1.0)
    first = next((i for i, ok in enumerate(marks) if ok), None)
    return {
        "q": q,
        "recall": recall,
        "rr": 0.0 if first is None else 1 / (first + 1),
        "top": [(_label(q["kind"], h), ok) for h, ok in zip(hits[:TOP_K], marks, strict=False)],
    }


def _score(rs: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "recall": round(sum(r["recall"] for r in rs) / len(rs), 3),
        "mrr": round(sum(r["rr"] for r in rs) / len(rs), 3),
        "queries": len(rs),
    }


def _grouped(results: list[dict[str, Any]], key: Any) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        for k in key(r["q"]):
            groups.setdefault(k, []).append(r)
    return {k: _score(rs) for k, rs in sorted(groups.items())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--check", action="store_true", help="exit 1 under the bar")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--json", type=Path, help="also write the scores to this file")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every top 5")
    args = parser.parse_args()
    if args.check and args.split != "all":
        parser.error("--check holds all queries to the bar; drop --split")

    queries = json.loads((FIXTURES / "retrieval_golden.json").read_text())["queries"]
    _check_golden(queries)
    if args.split != "all":
        queries = [q for q in queries if q["split"] == args.split]
    with httpx.Client(base_url=args.base_url, timeout=30, transport=_WaitOutRateLimit()) as client:
        started = time.perf_counter()
        results = [evaluate(client, q) for q in queries]
        seconds = time.perf_counter() - started

    print(f"{'recall@5':>8} {'RR':>5}  query")
    for r in results:
        q = r["q"]
        filters = ", ".join(f"{k}={v}" for k, v in q["params"].items())
        flag = "" if r["recall"] >= 0.6 and r["rr"] >= 0.5 else "  <--"
        print(f"{r['recall']:8.2f} {r['rr']:5.2f}  [{q['kind']}] {q['q']!r} {filters}{flag}")
        if args.verbose or flag:
            for label, ok in r["top"]:
                print(f"{'':16}{'✓' if ok else '·'} {label}")

    report = {
        "kind": _grouped(results, lambda q: [q["kind"]]),
        "tag": _grouped(results, lambda q: q["tags"]),
        "split": _grouped(results, lambda q: [q["split"]]),
        "overall": _score(results),
    }
    for section in ("kind", "tag", "split"):
        print()
        for name, s in report[section].items():
            print(
                f"{name:>12}: recall@5 {s['recall']:.3f}  MRR {s['mrr']:.3f}"
                f"  ({s['queries']} queries)"
            )
    overall = report["overall"]
    print(
        f"\n{'overall':>12}: recall@5 {overall['recall']:.3f}  MRR {overall['mrr']:.3f}"
        f"  ({overall['queries']} queries, {seconds:.1f}s)"
    )
    if args.json:
        report["queries"] = [
            {"q": r["q"]["q"], "kind": r["q"]["kind"], "recall": r["recall"], "rr": r["rr"]}
            for r in results
        ]
        args.json.write_text(json.dumps(report, indent=1) + "\n")

    recall, mrr = overall["recall"], overall["mrr"]
    if args.check and (recall < MIN_RECALL or mrr < MIN_MRR):
        sys.exit(f"Retrieval below the bar (recall@5 ≥ {MIN_RECALL}, MRR ≥ {MIN_MRR})")


if __name__ == "__main__":
    main()
