"""Retrieval eval (docs/week4-plan.md step 5, B4-D17, B4-D20): runs the golden
queries in scripts/fixtures/retrieval_golden.json against the search API and
reports recall@5 and MRR, per query, per kind, per tag, per split and overall,
with a 95% bootstrap range, plus latency and filter checks.

    uv run --group dev python scripts/eval_retrieval.py              # report
    uv run --group dev python scripts/eval_retrieval.py --check      # + fail under the bar
    uv run --group dev python scripts/eval_retrieval.py --split test --json out.json
    uv run --group dev python scripts/eval_retrieval.py --compare before.json

Each query has a split — dev (the original 32, which the fusion weights were
tuned on) or test (held out, for comparing models and settings fairly) — and
tags for what it exercises (exact, vague, attribute, roman_urdu, typo,
negation, multi, filter), so a change that helps one kind of query and hurts
another shows up instead of averaging out.

Kinds: item, restaurant, cuisine, review go to /v1/ai/search; `order` goes to
/v1/ai/search/history as the seeded demo customer (their 9 imported orders).
Queries tagged `unanswerable` (nothing on the menu fits) are printed, not
scored: search has no "no match" signal yet — RRF scores are rank-based, so
the first hit always looks confident.

Needs the seeded stack with the index built (`docker compose up`,
scripts/seed.py, `docker compose exec ai-service python -m app.reindex`).

- recall@5: of the relevant answers, the share in the top 5 — out of at
  most 5, so a query with 20 right answers isn't capped at 25%.
- MRR: 1 / the rank of the first relevant hit in the top 10 (0 if none),
  averaged — how high the first good answer comes.
- Range: the 95% bootstrap interval of the mean over queries (resampled
  2,000 times, fixed seed) — two runs whose ranges overlap a lot aren't
  really different. `--compare` does it properly: a paired bootstrap of the
  per-query differences against an earlier `--json` file.
- Violations: every hit is checked against its query's filters (price,
  dietary tags, spice, excluded words, cuisine, restaurant rating, radius,
  in stock, available, active restaurant; for order history, that the order
  is the caller's). Any violation fails `--check` —
  that's a correctness bug, not a ranking one.

Item names are matched as "Restaurant / Item", so the golden set doesn't
depend on database IDs; every name is checked against the catalog fixture
first, so a typo fails loudly instead of quietly lowering the score.
"""

import argparse
import json
import math
import random
import re
import statistics
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from seed import PASSWORD

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOP_K = 5
MRR_DEPTH = 10
# The seeded customer whose imported orders the `order` queries search.
DEMO_CUSTOMER = "customer@demo.smartfoodops.example"
# The bar `--check` (scripts/check.sh --with-ai) holds the index to, over the
# 128 scored queries: the measured baseline (recall@5 0.823, MRR 0.904 with
# bge-base on 2026-11-16, identical across runs; B4-D20) less ~0.05 — about the
# width of the bootstrap range, so a regression of a few queries fails but
# noise doesn't. Filter violations fail it regardless of the scores.
MIN_RECALL = 0.77
MIN_MRR = 0.85
BOOTSTRAP_SAMPLES = 2000
EARTH_KM = 6371.0088


class _WaitOutRateLimit(httpx.HTTPTransport):
    """The gateway allows 120 requests/min per IP; one eval is ~130, so runs
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
    known = {"item": items, "order": items, "restaurant": restaurants, "cuisine": cuisines}
    for q in queries:
        if "unanswerable" in q["tags"] and q["relevant"]:
            sys.exit(f"Golden set: {q['q']!r} is unanswerable but lists answers")
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
    if kind == "order":
        return f"{data['restaurant_name']}: {', '.join(data['items'])}"
    return str(hit["id"])


def _is_relevant(kind: str, hit: dict[str, Any], relevant: list[Any]) -> bool:
    if kind == "review":
        text = hit["text"].lower()
        return any(
            (r["restaurant"] is None or r["restaurant"] == hit["data"]["restaurant_name"])
            and any(word in text for word in r["any"])
            for r in relevant
        )
    if kind == "order":
        # "Restaurant / Item": an order from that restaurant with that item.
        data = hit["data"]
        return any(
            rel == f"{data['restaurant_name']} / {item}"
            for rel in relevant
            for item in data["items"]
        )
    return _label(kind, hit) in relevant


def _km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((p2 - p1) / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    )
    return 2 * EARTH_KM * math.asin(math.sqrt(a))


def _words(*texts: str | None) -> set[str]:
    """ai-service's app/documents.py `words` (folded plurals), restated so
    the check doesn't trust the code it checks."""
    out = set()
    for text in texts:
        for w in re.findall(r"[a-z]+", (text or "").lower()):
            if len(w) <= 1:
                continue
            if len(w) > 4 and w.endswith("es"):
                w = w[:-2]
            elif len(w) > 3 and w.endswith("s") and not w.endswith("ss"):
                w = w[:-1]
            out.add(w)
    return out


def _item_violations(
    item: dict[str, Any], params: dict[str, Any], ratings: dict[str, float | None]
) -> list[str]:
    """What's wrong with one item (an item hit, or a dish under a cuisine)."""
    name = item.get("name", "?")
    wrong = []
    if not item.get("is_available") or not item.get("in_stock"):
        wrong.append(f"{name}: not orderable")
    if "max_price" in params and item["price"] > params["max_price"]:
        wrong.append(f"{name}: price {item['price']} > {params['max_price']}")
    missing = set(params.get("dietary", [])) - set(item.get("dietary_tags", []))
    if missing:
        wrong.append(f"{name}: not {', '.join(sorted(missing))}")
    if "max_spice" in params and item["spice_level"] > params["max_spice"]:
        wrong.append(f"{name}: spice {item['spice_level']} > {params['max_spice']}")
    if banned := _words(*params.get("exclude", [])) & _words(
        item.get("name"), item.get("category"), item.get("description")
    ):
        wrong.append(f"{name}: mentions {', '.join(sorted(banned))}")
    if params.get("cuisine") and not _words(*params["cuisine"]) & _words(item.get("cuisine")):
        wrong.append(f"{name}: cuisine {item.get('cuisine')!r}")
    if "min_rating" in params:
        rating = ratings.get(str(item.get("restaurant_id")))
        if rating is None or rating < params["min_rating"]:
            wrong.append(f"{name}: restaurant rated {rating}")
    return wrong


def _violations(
    q: dict[str, Any],
    hits: list[dict[str, Any]],
    customer_id: str,
    ratings: dict[str, float | None],
) -> list[str]:
    """Hits that break the query's filters — must be none, whatever the rank."""
    params, wrong = q["params"], []
    for hit in hits:
        data = hit["data"]
        if q["kind"] == "order":
            if data.get("customer_id") != customer_id:
                wrong.append(f"order {hit['id']}: another customer's order")
            continue
        dishes = data["items"] if q["kind"] == "cuisine" else [data]
        for dish in dishes:
            if dish.get("restaurant_status") != "active":
                wrong.append(f"{dish.get('name', hit['id'])}: restaurant not active")
            if "lat" in params and dish.get("location"):
                km = _km(
                    params["lat"], params["lng"], dish["location"]["lat"], dish["location"]["lon"]
                )
                # 1% slack: Qdrant's distance and ours round differently.
                if km > params.get("radius_km", 5.0) * 1.01:
                    wrong.append(f"{dish.get('name', hit['id'])}: {km:.1f} km away")
            if q["kind"] in ("item", "cuisine"):
                wrong += _item_violations(dish, params, ratings)
    return wrong


def evaluate(
    client: httpx.Client,
    q: dict[str, Any],
    auth: dict[str, str],
    customer_id: str,
    ratings: dict[str, float | None],
) -> dict[str, Any]:
    if q["kind"] == "order":
        path, params, headers = "/v1/ai/search/history", {"q": q["q"]}, auth
    else:
        path, params, headers = "/v1/ai/search", {"q": q["q"], "kind": q["kind"], **q["params"]}, {}
    started = time.perf_counter()
    resp = client.get(path, params={**params, "limit": MRR_DEPTH}, headers=headers)
    seconds = time.perf_counter() - started
    resp.raise_for_status()
    hits = resp.json()["hits"]
    marks = [_is_relevant(q["kind"], h, q["relevant"]) for h in hits]
    # Reviews: any matching review counts, so the ideal is a full top 5.
    wanted = TOP_K if q["kind"] == "review" else min(len(q["relevant"]), TOP_K)
    recall = min(sum(marks[:TOP_K]) / wanted, 1.0) if wanted else 0.0
    first = next((i for i, ok in enumerate(marks) if ok), None)
    return {
        "q": q,
        "recall": recall,
        "rr": 0.0 if first is None else 1 / (first + 1),
        "seconds": seconds,
        "violations": _violations(q, hits, customer_id, ratings),
        "top": [(_label(q["kind"], h), ok) for h, ok in zip(hits[:TOP_K], marks, strict=False)],
    }


def _bootstrap(values: list[float], rng: random.Random) -> tuple[float, float]:
    """95% interval of the mean, resampling the queries."""
    n = len(values)
    means = sorted(sum(rng.choices(values, k=n)) / n for _ in range(BOOTSTRAP_SAMPLES))
    return means[int(0.025 * BOOTSTRAP_SAMPLES)], means[int(0.975 * BOOTSTRAP_SAMPLES) - 1]


def _score(rs: list[dict[str, Any]], ranges: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "recall": round(sum(r["recall"] for r in rs) / len(rs), 3),
        "mrr": round(sum(r["rr"] for r in rs) / len(rs), 3),
        "queries": len(rs),
    }
    if ranges:
        rng = random.Random(0)
        out["recall_range"] = [round(x, 3) for x in _bootstrap([r["recall"] for r in rs], rng)]
        out["mrr_range"] = [round(x, 3) for x in _bootstrap([r["rr"] for r in rs], rng)]
    return out


def _grouped(results: list[dict[str, Any]], key: Any) -> dict[str, dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        for k in key(r["q"]):
            groups.setdefault(k, []).append(r)
    return {k: _score(rs) for k, rs in sorted(groups.items())}


def _key(q: dict[str, Any]) -> str:
    return json.dumps([q["q"], q["kind"], q.get("params", {})], sort_keys=True)


def _compare(results: list[dict[str, Any]], path: Path) -> None:
    """Paired bootstrap of (now − before) over the queries both runs scored."""
    before = {_key(r): r for r in json.loads(path.read_text())["queries"]}
    pairs = [(r, before[_key(r["q"])]) for r in results if _key(r["q"]) in before]
    if not pairs:
        sys.exit(f"--compare: no queries in common with {path} (scored with params since B4-D20)")
    rng = random.Random(0)
    print(f"\nCompared with {path} ({len(pairs)} queries in both):")
    for metric, old in (("recall", "recall"), ("rr", "rr")):
        diffs = [now[metric] - prev[old] for now, prev in pairs]
        low, high = _bootstrap(diffs, rng)
        mean = sum(diffs) / len(diffs)
        verdict = "better" if low > 0 else "worse" if high < 0 else "no real difference"
        name = "recall@5" if metric == "recall" else "MRR"
        print(f"{name:>12}: {mean:+.3f}  (95% {low:+.3f} … {high:+.3f})  → {verdict}")
        changed = sorted(
            (
                (now[metric] - prev[old], now["q"]["q"])
                for now, prev in pairs
                if now[metric] != prev[old]
            ),
        )
        for d, q in changed[:3] + changed[-3:] if len(changed) > 6 else changed:
            print(f"{'':16}{d:+.2f}  {q!r}")


def _login(client: httpx.Client) -> tuple[dict[str, str], str]:
    resp = client.post("/v1/auth/login", json={"email": DEMO_CUSTOMER, "password": PASSWORD})
    if resp.status_code != 200:
        sys.exit(f"Can't log in as {DEMO_CUSTOMER} ({resp.status_code}): run scripts/seed.py")
    auth = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    me = client.get("/v1/users/me", headers=auth)
    me.raise_for_status()
    return auth, str(me.json()["id"])


def _ratings(client: httpx.Client) -> dict[str, float | None]:
    """Each restaurant's average rating, from restaurant-service (not the
    index), for the min_rating check."""
    resp = client.get("/v1/restaurants", params={"limit": 100})
    resp.raise_for_status()
    return {str(r["id"]): r["avg_rating"] for r in resp.json()["items"]}


def _percentile(values: list[float], p: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[int(p) - 1]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--base-url", default="http://localhost:8080")
    parser.add_argument("--check", action="store_true", help="exit 1 under the bar")
    parser.add_argument("--split", choices=["dev", "test", "all"], default="all")
    parser.add_argument("--json", type=Path, help="also write the scores to this file")
    parser.add_argument("--compare", type=Path, help="an earlier --json file to compare with")
    parser.add_argument("-v", "--verbose", action="store_true", help="show every top 5")
    args = parser.parse_args()
    if args.check and args.split != "all":
        parser.error("--check holds all queries to the bar; drop --split")

    queries = json.loads((FIXTURES / "retrieval_golden.json").read_text())["queries"]
    _check_golden(queries)
    if args.split != "all":
        queries = [q for q in queries if q["split"] == args.split]
    with httpx.Client(base_url=args.base_url, timeout=30, transport=_WaitOutRateLimit()) as client:
        auth, customer_id = _login(client)
        started = time.perf_counter()
        ratings = _ratings(client)
        all_results = [evaluate(client, q, auth, customer_id, ratings) for q in queries]
        seconds = time.perf_counter() - started

    results = [r for r in all_results if "unanswerable" not in r["q"]["tags"]]
    unanswerable = [r for r in all_results if "unanswerable" in r["q"]["tags"]]

    print(f"{'recall@5':>8} {'RR':>5}  query")
    for r in results:
        q = r["q"]
        filters = ", ".join(f"{k}={v}" for k, v in q["params"].items())
        flag = "" if r["recall"] >= 0.6 and r["rr"] >= 0.5 else "  <--"
        print(f"{r['recall']:8.2f} {r['rr']:5.2f}  [{q['kind']}] {q['q']!r} {filters}{flag}")
        if args.verbose or flag:
            for label, ok in r["top"]:
                print(f"{'':16}{'✓' if ok else '·'} {label}")
    if unanswerable:
        print("\nNot on the menu (not scored; what a customer would be shown):")
        for r in unanswerable:
            top = "; ".join(label for label, _ in r["top"][:3]) or "(nothing)"
            print(f"{'':6}[{r['q']['kind']}] {r['q']['q']!r} → {top}")

    violations = [(r["q"]["q"], v) for r in all_results for v in r["violations"]]
    latencies = [r["seconds"] * 1000 for r in all_results]
    report: dict[str, Any] = {
        "kind": _grouped(results, lambda q: [q["kind"]]),
        "tag": _grouped(results, lambda q: q["tags"]),
        "split": _grouped(results, lambda q: [q["split"]]),
        "overall": _score(results, ranges=True),
        "latency_ms": {
            "p50": round(_percentile(latencies, 50), 1),
            "p95": round(_percentile(latencies, 95), 1),
        },
        "violations": len(violations),
    }
    for section in ("kind", "tag", "split"):
        print()
        for name, s in report[section].items():
            print(
                f"{name:>12}: recall@5 {s['recall']:.3f}  MRR {s['mrr']:.3f}"
                f"  ({s['queries']} queries)"
            )
    overall = report["overall"]
    rl, rh = overall["recall_range"]
    ml, mh = overall["mrr_range"]
    print(
        f"\n{'overall':>12}: recall@5 {overall['recall']:.3f} ({rl:.3f}–{rh:.3f})"
        f"  MRR {overall['mrr']:.3f} ({ml:.3f}–{mh:.3f})"
        f"  ({overall['queries']} scored queries, {seconds:.1f}s)"
    )
    print(
        f"{'latency':>12}: p50 {report['latency_ms']['p50']:.0f} ms"
        f"  p95 {report['latency_ms']['p95']:.0f} ms  (through the gateway)"
    )
    print(f"{'violations':>12}: {len(violations)}")
    for q, v in violations[:20]:
        print(f"{'':14}{q!r}: {v}")

    if args.json:
        report["queries"] = [
            {
                "q": r["q"]["q"],
                "kind": r["q"]["kind"],
                "params": r["q"]["params"],
                "recall": r["recall"],
                "rr": r["rr"],
                "ms": round(r["seconds"] * 1000, 1),
            }
            for r in results
        ]
        args.json.write_text(json.dumps(report, indent=1) + "\n")
    if args.compare:
        _compare(results, args.compare)

    recall, mrr = overall["recall"], overall["mrr"]
    if args.check and violations:
        sys.exit(f"{len(violations)} hit(s) break their query's filters")
    if args.check and (recall < MIN_RECALL or mrr < MIN_MRR):
        sys.exit(f"Retrieval below the bar (recall@5 ≥ {MIN_RECALL}, MRR ≥ {MIN_MRR})")


if __name__ == "__main__":
    main()
