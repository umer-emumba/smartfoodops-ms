from prometheus_client import Counter

# How order sagas end (week3-plan step 5): delivered, or rejected/cancelled
# and why. `system_error` means retries ran out on an infrastructure failure
# — it has an alert (infra/prometheus/rules.yml).
SAGA_OUTCOMES = Counter(
    "saga_outcomes_total", "Orders reaching a terminal status", ["status", "reason"]
)
