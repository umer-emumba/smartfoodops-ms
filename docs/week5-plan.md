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

## 2. Build order

| Step | Work | Done when |
|---|---|---|
| **0 Foundations** | `app/llm.py` + error mapping + price table; config rename to `llm_*` (`env.example` only); `ai-db` migration (conversations, turns, interactions, drafts, outbox); versioned `app/prompts/`; generic gateway SSE proxy + AI rate-limit bucket | Unit tests with the fake model pass; the gateway proxies a test stream |
| **1 Retrieval for the assistant** | Filters `max_spice`, `cuisine`, `exclude_tags`, `min_rating`; restaurant suggestions (match × rating × load); "no good match" threshold tuned on the not-on-the-menu queries | Retrieval eval stays above the bar (0.77 / 0.85), 0 filter violations, not-on-the-menu queries flagged |
| **2 Assistant graph** | understand → route → retrieve (dishes / restaurants / history in parallel) → pick → check → explain; one retry, else retrieval-only cards; page context; memory (last N turns + summary) | Assistant eval: intent ≥ 90%, constraints correct, 0 made-up IDs |
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
