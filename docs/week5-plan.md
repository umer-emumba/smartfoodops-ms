# Week 5 Plan — AI Assistant + Streaming (Part B)

## Context

Week 4 built the retrieval layer: ai-service, Qdrant, the event-driven indexer and hybrid search
(`app/retrieval.py::retrieve`), measured by a 134-query eval (B4-D1…D21). Week 5 adds what
`SmartFoodOps - Part B.pdf` and Guidelines §6 ask for on top of it: a contextual food
recommendation assistant, an order explanation system (delay, cancellation, ETA), a restaurant
suggestion engine, streaming responses, prompt engineering, restaurant content generation, AI
analytics and AI observability.

Working rule: build step by step, verify each step in Docker, and ask before committing. All
steps go on `week5/ai-assistant`, cut from `week4/retrieval-layer` (325be05). The LLM key is
added to `services/ai-service/.env` by the user (`LLM_PROVIDER`, `LLM_API_KEY`); tools never
edit `.env`.

### What Week 4 left for Week 5, and what the code is missing

| Finding | Effect on Week 5 |
|---|---|
| Search fails on negation (recall 0.05) and Roman Urdu (0.64), and can't say "no match" (B4-D18, B4-D20) | The assistant rewrites the question and turns negations into filters; a "no good match" check is added |
| `Filters` has no spice level, cuisine, exclusions or minimum rating | Add them before the graph (step 1) |
| The PDF asks for a restaurant suggestion engine; retrieval only ranks by text | Restaurant suggestions ranked by match + rating + current kitchen load |
| "What should I eat tonight under **$10**?" — prices are PKR with no currency field | Convert with a fixed rate from config and say so in the answer |
| Restaurants have no opening hours, only a concurrent-order cap | "Tonight" can't be checked; use "active + not overloaded" and record the gap |
| "From **this** restaurant" / "why is **my** order late" depend on the page the customer is on | Chat requests carry page context (`restaurant_id`, `order_id`); no order given → the latest active order |
| The gateway gives ai-service a 15 s timeout; the only streaming route is tracking | Make the streaming proxy generic; AI chat gets its own route and timeout |
| ai-service has no Redis | Conversation memory lives in `ai-db` |

## 1. Design decisions

| # | Decision | Why |
|---|---|---|
| B5-D1 | **The LLM provider is set by config** (decided 2026-11-02): `llm_provider` (`anthropic` \| `openai`), `llm_api_key` (replaces `anthropic_api_key`), `llm_model_smart` / `llm_model_fast` (defaults `claude-sonnet-5-5` / `claude-haiku-5-5`, checked against current docs at build time), optional `llm_base_url`. `app/llm.py` is the only module that names a provider; everything else gets a `BaseChatModel` by role. Provider errors map in one place to `LLMRateLimited`, `LLMUnavailable`, `LLMAuthError`, `LLMBadOutput`. Tests use LangChain's fake chat model | Switching provider costs days, not weeks; CI needs no key |
| B5-D2 | **Fixed LangGraph pipeline, not a free-running agent loop** | Predictable, each step testable, cost and latency bounded |
| B5-D3 | **Pick → check → explain.** The smart model picks dish/restaurant IDs with structured output; code checks them; cards are streamed first; then the explanation streams, written only from checked cards | No unchecked dish ever reaches the screen; cards first feels faster |
| B5-D4 | **One fast-model call does intent + constraints + query rewrite** (Roman Urdu → English, negation → filters, `$` → PKR) | One round trip saved per message; fixes Week 4's weak spots where they belong |
| B5-D5 | **The model reasons, code does the arithmetic**: budget totals, combos from one restaurant, live price and availability are checked in code | LLMs get sums wrong; code doesn't |
| B5-D6 | **Order explanations = facts + rules in code; the LLM only words them** | No invented reasons for a delay, cancellation or refund |
| B5-D7 | **Restaurant content is saved as drafts only**; on accept the UI calls restaurant-service with the admin's own token | ai-service never writes the catalog and needs no admin rights |
| B5-D8 | **Privacy**: no names, phone numbers or addresses go to the LLM — only coordinates resolved server-side. Interactions are stored as metadata plus redacted text, with a retention period | Confidential data stays in our services; prompts are smaller |
| B5-D9 | **Untrusted text is fenced**: reviews and user text are wrapped in tags marked as data; the system prompt says instructions inside them are ignored; a drill tests it | Reviews are user-written and go straight into prompts |
| B5-D10 | **Limits**: per-user AI rate limit at the gateway (own bucket), `max_tokens` on every call, a per-turn token budget, a cost metric from a per-model price table | No runaway spend |
| B5-D11 | **Structured output uses the provider's `json_schema` mode, not a forced tool call**; the smart → fast fallback only fires for transient errors (rate limit, outage, timeout), never for a bad key or a bad request. Provider errors are mapped by HTTP status, not SDK class. OpenAI has no default model names in `app/llm.py` — they must be set | Sonnet 5.5 rejects forced tool use. A bad key fails the same on both models. Mapping by status covers both SDKs and any OpenAI-compatible server. Unchecked model names would be guesses |
| B5-D12 | **Tables come with the step that uses them**: step 0 adds `conversations`, `conversation_turns`, `ai_interactions`; content drafts arrive in step 5 and the outbox in step 6 | Same rule as Week 4 (`app/models.py`): a table's shape is decided when its first user is written, not guessed ahead |
| B5-D13 | **Gateway**: one SSE proxy (`_stream_upstream`) serves tracking and AI. AI streams (`POST /v1/ai/chat`, `GET /v1/ai/orders/{id}/explain`) get their own pool (`ai-service-stream`, 200 connections, 45 s read timeout = 3 missed heartbeats) and their own rate-limit bucket (`ai`, 20/min per user). Search stays on the buffered catch-all and the default bucket | Slow LLM answers can't starve search; spending the AI budget doesn't touch the rest of the app |
| B5-D14 | **"No good match" is decided by the pick step (step 2), not a score threshold.** Measured on the 6 not-on-the-menu queries: top dense cosine 0.505–0.556 while real queries spread 0.450–0.9 ("sushi" 0.536 > "haleem" 0.523); a cross-encoder (ms-marco-MiniLM-L-6, 158 ms) gives "sushi" −4.8 and "quick snack between meetings" −10.8. The model sees the candidates and may return none; the assistant eval checks it | A threshold that can't separate the two would hide real answers or show wrong ones; the pick call happens anyway, at no extra cost |
| B5-D15 | **Assistant filters live in the index payload** (index revision r2): `cuisine_words` and `words` (name, category, description), plural-folded keyword fields. `exclude` is "no dish mentioning the word" (it can over-exclude, e.g. "nut-free" for "nut" — the safe side); allergens go through dietary tags. `min_rating` on dishes is resolved by one scroll of rated restaurants first, so the rating isn't copied onto every dish | Filters stay inside the Qdrant query (B4 rule); text and vectors are unchanged |
| B5-D16 | **Kitchen load is read live** from restaurant-service (`POST /internal/restaurants/load`, 1 s timeout) per suggestion request — not indexed, it changes with every order. Score = match × rating factor (0.6–1.0, Bayesian average) × load factor (1 − 0.5 × utilization); full or closed kitchens are left out. Any failure = load unknown, ranked as idle, `load: null` | A ranking hint must never fail a suggestion; ordering checks capacity itself |
| B5-D17 | **The model-facing schemas are flat and unconstrained**: `budget_total`, `max_price_each`, `currency` instead of a nested `Budget` object; no numeric or length limits in the schema. `Understanding.cleaned()` clamps in code (money 0–1e6, people 1–20, spice 0–3, rating < 1 → none, ≤ 3 cuisines, ≤ 8 exclusions, query ≤ 120 chars) | qwen3:8b returned null for the nested `anyOf[$ref, null]` budget (5/10 filters → 10/10 once flat); schema limits are enforced unevenly by providers, code limits always are |
| B5-D18 | **When pick fails twice, code builds the answer**: options = the top-ranked candidates that pass the check; a meal = one restaurant, its best-ranked dishes that fit the budget, up to people + 1 lines (`fallback_meal`). Status `degraded` | The first fallback (top 3 by rank) broke the meal rules (several restaurants, over budget) — the fallback must pass the same check as the model |
| B5-D19 | **Reasoning effort per role, with two special values**: `omit` = don't send the parameter (provider default), `none` = send "none" (turns qwen3 thinking off: 13 s → 2.8 s per call, same answer). gpt-oss on Groq: `low` | Thinking is the largest token and latency cost on small and reasoning models; the right setting depends on the provider |
| B5-D20 | **Tokens are logged per graph step** (`ai_interactions.steps`, migration 0003): calls, input, cached, output, reasoning, ms for understand / pick / explain; the step is LangGraph's node name, read by the usage callback. The eval prints the per-step average | One total per turn can't say where tokens go. First measurement (Groq, gpt-oss): pick 2,970 input of 5,342 (56%) |
| B5-D21 | **Lists the model fills in may come back null**: in the JSON schema, lists of plain values (`dietary`, `cuisines`, `exclude`, `restaurant_ids`) are `["array", "null"]`, and null is read as `[]`; `understand@v2` asks for `[]`. Lists of objects (`Pick.items`) stay arrays in the schema (B5-D17) but also read null as `[]` | gpt-oss-20b on Groq answered `"dietary": null` in 2 of 6 calls; Groq rejects output that breaks the schema (400), the turn fell back to retrieval-only cards and ignored "no beef". After: 8 of 8 OK |
| B5-D22 | **Invented filters: no check yet, decided by measurement.** qwen3:8b once added filters nobody asked for (vegetarian, indian, mild, rated 4+ to "lunch for 2 under Rs 2000, no beef" → nothing found); gpt-oss on Groq didn't in ~12 calls. A keyword check was built and removed (word lists to maintain). If the eval on the demo model shows it, add **evidence quotes**: the model returns the customer's words for each filter, code drops a filter whose words aren't in the messages | Seen once, on a small local model; a fix with word lists costs upkeep, and the better fix (quotes) should be measured first |
| B5-D23 | **Pick answers with short codes, not IDs**: candidates are shown as `[D1]…[Dn]` (dishes) or `[R1]…[Rn]` (restaurants); code maps the answer back to real IDs before the check. Codes live for one turn only (same on a retry), never stored or put in history. Anything that isn't a code is still caught as made up. `pick@v2` | gpt-oss changed one character of a 36-character ID in 1 of 8 Groq runs (caught, but cost a retry); a code can't be miscopied. Pick input 2,970 → ~2,235 tokens per call on the same question |
| B5-D24 | **One turn at a time per conversation**: `conversations.busy_until` is claimed by one `UPDATE … WHERE busy_until IS NULL OR < now()` (database time, any number of replicas), cleared when the turn is saved, and expires after `assistant_turn_lock_seconds` (120 s) if the process dies. A second message meanwhile gets 409; a new conversation is never blocked (migration 0005) | Allowing it would run the second turn without the first answer in its history ("something cheaper" than what?), save turns out of order and double the cost. A held DB lock would pin a connection for the whole stream; Redis would be a new dependency for ai-service |
| B5-D25 | **A client that disconnects cancels the turn**: the graph runs in its own task, cancelled when the stream stops — with it the LLM call. The turn is saved (shielded from the cancel) as `interrupted`, error code `client_gone`, with the text streamed so far. A failure mid-answer ends the stream with an `error` event and is saved as `error`; errors known before the stream (401/403/404/409/422) are plain HTTP | No tokens spent on answers nobody reads; the history shows what the customer actually saw; the conversation is freed either way |
| B5-D26 | **Order facts are read as the customer**: ai-service calls order-, delivery- and payment-service's public APIs with the caller's own bearer token (`app/order_client.py`). Not found / not yours → the same 404 / 403; order-service down → 503; a missing delivery or payment only means fewer facts. Customers only. The token is held in memory for the turn, never stored or sent to a model | Ownership is exactly `GET /v1/orders/{id}`'s, with no new internal endpoints and no copied rules |
| B5-D27 | **The number guard**: every number in the model's explanation (amounts, minutes, times, the order number) must appear in the facts, else the code-written explanation is shown (`order_words.py`, metric `ai_order_explanations_total{outcome}`). So the 2–3 sentences are generated in full, checked, then streamed in small chunks (~1 s) | A wrong ETA or amount can't be taken back once streamed; the model once did its own sum in a chat answer ("Rs 1,900" for a Rs 1,700 meal) |
| B5-D28 | **Situations are decided by rules** (`order_facts.py`, 14 codes): waiting for the restaurant, preparing, kitchen late (> 1 min past `ready_at`, food not ready — also when a rider is already assigned), finding a rider, rider late, on the way, running late, delivered / delivered late, cancelled, rejected, payment declined, refunded. Kitchen load is a fact only when ≥ 80 % full. Notes people typed (rejection, cancellation, refund) are kept apart and fenced. Times in Pakistan time (UTC+5) | The model words, never decides (B5-D6). A quiet kitchen's "1 of 20 orders" was read by the model as an excuse; "cancelled by us" came from an ambiguous fact — both fixed in the facts, not just the prompt |

## 2. Build order

| Step | Work | Done when |
|---|---|---|
| **0 Foundations** | `app/llm.py` + error mapping + price table; config rename to `llm_*` (`env.example` only); `ai-db` migration (conversations, turns, interactions, drafts, outbox); versioned `app/prompts/`; generic gateway SSE proxy + AI rate-limit bucket | Unit tests with the fake model pass; the gateway proxies a test stream |
| **1 Retrieval for the assistant** | Filters `max_spice`, `cuisine`, `exclude`, `min_rating`; restaurant suggestions (match × rating × load); ~~"no good match" threshold~~ (moved to step 2, B5-D14) | Retrieval eval stays above the bar (0.77 / 0.85), 0 filter violations |
| **2 Assistant graph** | understand → route → retrieve (dishes / restaurants / history in parallel) → pick → check → explain; pick may return no match (B5-D14); one retry, else retrieval-only cards; page context; memory (last N turns + summary) | Assistant eval: intent ≥ 90%, constraints correct, 0 made-up IDs, not-on-the-menu questions answered "not on the menu" |
| **3 Streaming** | `POST /v1/ai/chat` SSE: `status → cards → token… → done \| error`; heartbeats; client disconnect cancels the LLM call; time-to-first-token metric | `smoke_test.py --assistant`: stream completes, every card exists in the catalog |
| **4 Order explanations** | Facts from order summary + status history, delivery by-order, payment by-order, restaurant load; rules in code; covers delay, ETA, restaurant load, refund, cancellation; same ownership rules as `GET /v1/orders/{id}`; also `GET /v1/ai/orders/{id}/explain` (streamed) | Seeded delayed, cancelled and refunded orders each get the right reason |
| **5 Restaurant content** | Menu descriptions, promotions (shaped like `Promotion`), dish picks, engagement messages, feedback summaries (summarise review batches, then combine); Celery tasks; draft → accept / reject | Drafts saved; accept goes through restaurant-service; owner-only |
| **6 AI analytics** | `ai-events` topic via the ai-service outbox (`AssistantQueryAnswered`, `RecommendationShown`, `RecommendationAccepted`); `OrderPlaced.ai_interaction_id`; `GET /v1/analytics/ai` with all six PDF metrics | Numbers match `ai-db` counts |
| **7 Observability + drills** | OTel span per graph step; metrics: LLM latency, time to first token, errors by type, interrupted streams, IDs caught by the check, empty retrievals; Grafana "AI Assistant" dashboard + alerts on LLM error rate and p95 latency; drills: bad key, Qdrant down, client disconnect, injection in a review | All four PDF observability items visible in Grafana / Jaeger |
| **8 Demo UI + docs** | Chat panel with cards, "Why is my order late?" button, review form, AI content page, AI analytics tab; PRD Part B (use cases, FR / NFR, milestones, traceability), README AI flow diagram, technical documentation, PROGRESS | Browser-checked; Guidelines §3 checklist met |

**Step 0 — done (2026-11-17).**
- **Built:** `app/llm.py` (models by role, error mapping, fallback, usage/cost callback); `app/prompts/` (`assistant_rules@v1`, `fence()`); `app/privacy.py` (`redact()`); ai-db migration 0001; LLM metrics; gateway stream routes.
- **Dependencies:** langchain 1.5, langchain-anthropic 1.7.10, langchain-openai 1.8. langchain-core pins `uuid-utils` < 1, so the shared lock moved it from 1.0.0 to 0.19.0. `sfo_common.ids.new_id()` only uses `uuid7()`, which 0.19 has, and every service's tests pass.
- **Measured:** ai-service image 1.1 → 1.2 GB; ai-service uses 450 MiB at rest.
- **Verified:** smoke `--deep` 49/49, `--ai` 45/45; `alembic check` reports no drift; the AI stream routes reach ai-service with per-user `ai` rate-limit keys.

**Step 1 — done (2026-11-18).**
- **Built:** `Filters.max_spice / cuisines / exclude / min_rating` (search API params too); `GET /v1/ai/restaurants/suggest` (public); restaurant-service `POST /internal/restaurants/load`; `app/restaurant_client.py`; index r2 (B5-D15, D16). Reindex: 2 min.
- **Gateway bug fixed (from Week 1):** the proxy forwarded only the last value of a repeated query key, so `dietary=halal&dietary=vegan` applied one tag. Now all are forwarded (test fails on the old code).
- **Measured:** eval 132 scored queries recall@5 0.827, MRR 0.910 (the original 128: 0.827 / 0.912 vs 0.823 / 0.904 at r1), 0 violations. Negation rewritten as filters: "chicken" + max_spice 1 → 1.00, "mild curry" + max_spice 1 → 1.00, "vegetarian meal" + exclude salad → 0.40 (the plain-text versions score ~0.05). Load check: Golden Dragon at 18/20 orders drops 0.909 → 0.50 and out of the top 3; at 20/20 it isn't suggested; restaurant-service down → suggestions still served with `load: null`.
- **Verified:** check.sh --with-db green; smoke `--ai` 48/48, `--deep` 49/49.

**Step 2 — built (2026-11-18); full eval pending.**
- **Built:** `app/assistant/` — LangGraph graph (understand → reply | retrieve → nothing / search_down / pick → check → retry pick once → explain), code check (`check.py`: made-up IDs, live price and stock via restaurant-service `quote`, kitchen load, budget, spice, one restaurant per meal), code fallbacks at every step, memory (last 6 turns + summary folded after `done`, turn `meta` with the last search), per-turn usage (`usage_scope()`), CLI `python -m app.assistant`. Prompts `understand`, `pick`, `explain`, `summarize` @v1. Migrations 0002 (turn meta, summary mark), 0003 (per-step usage). Decisions B5-D17…D23.
- **Eval:** `services/ai-service/evals/run.py` + `assistant_golden.json` (42 cases; kept in the service, not `scripts/`, because it runs the graph in-process). Bar: intent 0.90, filters 0.85, cards 0.85, 0 violations.
- **Measured** (negation + budget subset, 10 cases, qwen3:8b on Ollama, thinking off): intent 1.0, filters 1.0, cards 0.9, 0 violations, p50 65 s, ~4.7k tokens per case. Dish choice is weak (a "dessert" answer had no desserts). One Groq turn (gpt-oss-120b / 20b): 5.0 s, first card 4.4 s, 5.3k input / 730 output tokens.
- **Known gaps:** small models may invent filters (B5-D22); the pick model miscopied a 36-character ID (1 of 8 Groq runs) — fixed with short codes (B5-D23); check reasons aren't logged yet (a Groq turn retried pick for an unknown reason); full eval on a strong model (Claude) not run yet.
- **Verified:** 127 tests (22 with the DB) pass, ruff + mypy clean; tests no longer read the local `.env`.

**Step 3 — done (2026-11-19).**
- **Built:** `POST /v1/ai/chat` (`app/api/chat.py`): customers only, the caller from the token; SSE `status → cards → token… → done | error`; `: heartbeat` every 15 s while quiet; summary after the stream closes. `app/assistant/service.py` split into `start_turn` (claim + memory, before any byte) and `stream_turn` (run + save, always). Metric `ai_assistant_first_output_seconds`. Migration 0005. Decisions B5-D24, D25.
- **Measured** (Groq, gpt-oss-120b / 20b, through the gateway): first output 3.5–3.9 s, full answer 4.2–5.1 s on unthrottled turns. Quick successive turns hit Groq's free-tier limit (~8k tokens/min per model): understand waited 9–14 s on retries (first output 11–16 s) — a tier limit, not the code.
- **Verified:** `smoke_test.py --assistant` 44/44 (event order; every card a real, available catalog item at its current price; follow-up on the same conversation; 409 while answering; 401 / 404 / 422). A real disconnect (the smoke script's own crash on the first run) was saved as `interrupted` and freed the conversation. 14 new tests (146 pass, 37 with the DB): heartbeat, disconnect cancels the search / LLM call, error event without internals, two messages at once → one 409.

**Step 4 — done (2026-11-19).**
- **Built:** `GET /v1/ai/orders/{id}/explain` (customers only; `status → facts → token… → done`; logged as `order_explain`); `app/order_client.py` (B5-D26); `app/assistant/order_facts.py` (B5-D28); `app/assistant/order_words.py` + prompt `order_explain@v1` (B5-D27). The chat's "where is my order?" (intent `order_help`) now explains the customer's latest order the same way (graph node `order_help`); without a token (CLI) it keeps the fixed reply.
- **Verified:** `smoke_test.py --explain` 54/54 with real orders made through the API — delivered then partly refunded → `refunded`, restaurant declined → `rejected`, payment declined → `rejected`, customer cancelled → `cancelled`, kept past its ready time (prep 1 min, riders off shift) → `kitchen_late`; facts matched, no number outside the facts. 27 new tests (173 pass, 40 with the DB).
- **Fixed on the way:** a kitchen late with a rider already waiting was reported as "rider assigned"; two misleading wordings (B5-D28).

**If the week runs short** (agreed 2026-11-17): steps 0–4, the step 7 metrics and the step 8
docs are must-haves (they cover all four PDF deliverables). Cut in this order: step 5, then
step 6, then the extra UI pages (AI content, AI analytics).

## 3. Targets (non-functional)

- First `status` event < 300 ms; first card p95 < 4 s; full answer p95 < 10 s.
- 0 made-up IDs reach the customer (guaranteed by the check, shown by the eval).
- Every model call has `max_tokens`; the measured cost per turn is documented.
- LLM or Qdrant failure → retrieval-only cards or a "degraded" status, never a 500. Ordering
  flows never wait on ai-service.

## 4. Assistant eval (`scripts/eval_assistant.py`)

- ~40 golden questions (`scripts/fixtures/assistant_golden.json`): the PDF's examples, vague,
  Roman Urdu, negation, budget, combos, not on the menu, injection, order "why" cases.
- Checks in code (free): intent, extracted filters, card IDs and prices valid, budget respected,
  refusal when nothing matches.
- Optional live run with an LLM judge for groundedness — on demand, with a cost cap.
- A provider switch = change config, run the eval, compare.

## 5. Risks

| Risk | Mitigation |
|---|---|
| 2–3 model calls make answers slow | Cards stream first; the explanation uses the fast model; measure, merge calls if needed |
| API cost during development | Fake model in unit tests; live eval only on demand |
| The week is too full | Cut in the agreed order (§2) |
| Memory: stack ~4.9 GiB of a 7.8 GiB Docker limit | ai-service only adds HTTP clients; measure with `docker stats` after step 3 |

## 6. Week 5 deliverables (Part B PDF)

| Deliverable | Step |
|---|---|
| Fully functional AI assistant | 2–3 |
| Context-aware recommendation system | 1–2 (filters, history, page context) |
| Streaming AI responses | 3 |
| End-to-end GenAI integration | 4–8 |
