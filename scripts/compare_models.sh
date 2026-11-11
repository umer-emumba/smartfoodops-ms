#!/usr/bin/env bash
# Compares dense embedding models on the retrieval eval (docs/week4-plan.md
# B4-D18). For each model: build its own index (collections are named after
# the model, so the live bge-small index is untouched), restart ai-service on
# it, run scripts/eval_retrieval.py, and record index time, query-embedding
# time and memory. ai-service goes back to the default model at the end.
#
#   scripts/compare_models.sh                        all candidates
#   scripts/compare_models.sh BAAI/bge-base-en-v1.5  only these
#
# Needs the seeded stack up (`docker compose up`). Models download once into
# the `ai-model-cache` volume (docker-compose.models.yml). Results land in
# $OUT (default /tmp/sfo-model-compare). Every model indexes into revision-900
# collections (`*_r900`), deleted at the end; the live `_r1` index is untouched.
set -euo pipefail
cd "$(dirname "$0")/.."

OUT=${OUT:-/tmp/sfo-model-compare}
mkdir -p "$OUT"
COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.models.yml)
GATEWAY=http://localhost:8080

# model;dim;query prefix;document prefix — the prefixes each model was trained
# with (fastembed adds none of them).
BGE_QUERY="Represent this sentence for searching relevant passages: "
CANDIDATES=(
    "BAAI/bge-small-en-v1.5;384;$BGE_QUERY;"
    "BAAI/bge-base-en-v1.5;768;$BGE_QUERY;"
    "snowflake/snowflake-arctic-embed-s;384;$BGE_QUERY;"
    "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2;384;;"
    "google/embeddinggemma-300m;768;task: search result | query: ;title: none | text: "
)

selected=()
for entry in "${CANDIDATES[@]}"; do
    if [[ $# -eq 0 ]] || [[ " $* " == *" ${entry%%;*} "* ]]; then
        selected+=("$entry")
    fi
done
[[ ${#selected[@]} -gt 0 ]] || { echo "no candidate matches: $*" >&2; exit 2; }

wait_healthy() {
    local id
    id=$(docker compose ps -q ai-service)
    for _ in $(seq 1 60); do
        [[ $(docker inspect -f '{{.State.Health.Status}}' "$id") == healthy ]] && return 0
        sleep 2
    done
    echo "ai-service didn't become healthy" >&2
    exit 1
}

restore_default() {
    echo "== restoring ai-service to the default model, dropping the _r900 collections"
    docker compose up -d --no-deps ai-service >/dev/null
    wait_healthy
    for c in $(curl -s localhost:6333/collections | python3 -c \
        'import json,sys; print(*[c["name"] for c in json.load(sys.stdin)["result"]["collections"] if c["name"].endswith("_r900")])'); do
        curl -s -X DELETE "localhost:6333/collections/$c" >/dev/null
    done
}
trap restore_default EXIT

# The volume starts root-owned; the service runs as uid 10001.
export COMPARE_DENSE_MODEL=x COMPARE_DENSE_DIM=0
"${COMPOSE[@]}" run --rm --no-deps --user 0 --entrypoint chown ai-service -R 10001 /opt/models

for entry in "${selected[@]}"; do
    IFS=';' read -r model dim query_prefix document_prefix <<<"$entry"
    slug=${model##*/}
    export COMPARE_DENSE_MODEL=$model COMPARE_DENSE_DIM=$dim
    export COMPARE_QUERY_PREFIX=$query_prefix COMPARE_DOCUMENT_PREFIX=$document_prefix
    echo "== $model"

    # One copy of the model in memory at a time: the reindex runs with the
    # service stopped (it also downloads the model on first use).
    "${COMPOSE[@]}" stop ai-service >/dev/null
    started=$(date +%s)
    "${COMPOSE[@]}" run --rm --no-deps ai-service python -m app.reindex >"$OUT/$slug.reindex.log" 2>&1
    index_seconds=$(($(date +%s) - started))
    tail -1 "$OUT/$slug.reindex.log"

    "${COMPOSE[@]}" up -d --no-deps ai-service >/dev/null
    wait_healthy
    until curl -sf "$GATEWAY/v1/ai/search?q=warm+up" >/dev/null; do sleep 2; done

    uv run --frozen --group dev python scripts/eval_retrieval.py --json "$OUT/$slug.json" \
        >"$OUT/$slug.eval.log"
    tail -1 "$OUT/$slug.eval.log"

    memory=$(docker stats --no-stream --format '{{.MemUsage}}' "$(docker compose ps -q ai-service)")
    embed_ms=$(docker compose exec -T ai-service python -c '
import re, urllib.request
text = urllib.request.urlopen("http://localhost:9100/metrics").read().decode()
total = lambda s: float(re.search(r"ai_embed_seconds_" + s + r"\{purpose=\"query\"\} (\S+)", text).group(1))
print(round(1000 * total("sum") / total("count"), 1))')
    python3 - "$OUT/$slug.json" "$model" "$dim" "$index_seconds" "$embed_ms" "${memory%% /*}" <<'EOF'
import json, sys
path, model, dim, index_seconds, embed_ms, memory = sys.argv[1:]
report = json.load(open(path))
report["run"] = {"model": model, "dim": int(dim), "index_seconds": int(index_seconds),
                 "query_embed_ms": float(embed_ms), "memory": memory}
json.dump(report, open(path, "w"), indent=1)
EOF
done

python3 - "$OUT" "${selected[@]}" <<'EOF'
import json, sys
from pathlib import Path
out, entries = Path(sys.argv[1]), sys.argv[2:]
rows = [json.load(open(out / f"{e.split(';')[0].rsplit('/', 1)[-1]}.json")) for e in entries]
tags = sorted({t for r in rows for t in r["tag"]})
cols = [("test", lambda r: r["split"]["test"]), ("overall", lambda r: r["overall"])] + [
    (t, lambda r, t=t: r["tag"][t]) for t in tags]
print("\nrecall@5 / MRR (test = the held-out split)\n")
print(f"{'model':<40}" + "".join(f"{c:>14}" for c, _ in cols))
for r in rows:
    print(f"{r['run']['model'].rsplit('/', 1)[-1]:<40}"
          + "".join(f"{f(r)['recall']:>7.3f}/{f(r)['mrr']:.3f}"[-14:].rjust(14) for _, f in cols))
print(f"\n{'model':<40}{'dim':>6}{'index s':>9}{'query embed ms':>16}{'memory':>12}")
for r in rows:
    run = r["run"]
    print(f"{run['model'].rsplit('/', 1)[-1]:<40}{run['dim']:>6}{run['index_seconds']:>9}"
          f"{run['query_embed_ms']:>16}{run['memory']:>12}")
EOF
