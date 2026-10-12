# SmartFoodOps — Diagrams

Mermaid diagrams, rendered natively by GitHub — kept as text so they can't silently drift
out of sync with the code the way an exported image would. See
[architecture.md](./architecture.md) for the prose description of everything here.

## Microservices architecture

```mermaid
graph TD
    Client[Client apps] --> GW1[api-gateway]
    Client -.->|"prod only: Kubernetes Service load-balances replicas"| GW2[api-gateway replica N]

    GW1 -->|REST| US[user-service]
    GW1 -->|REST| RS[restaurant-service]
    GW1 -->|REST| OS[order-service]
    GW1 -->|REST| DS[delivery-service]
    GW1 -->|REST| PS[payment-service]
    GW1 -->|REST| NS[notification-service]
    GW1 ==>|"SSE streaming proxy"| TS[tracking-service]
    GW1 -->|deny-list check + rate limit| Redis[(Redis)]
    US -->|deny-list write on block| Redis
    RS -->|"menu cache; restaurant owner key"| Redis

    OS -->|sync REST: quote at order creation| RS
    OS -->|"saga workflow + order-activities worker"| Temporal[(Temporal server)]
    RS -->|restaurant-activities worker| Temporal
    PS -->|payment-activities worker| Temporal
    DS -->|"delivery-activities worker"| Temporal

    OS -->|"PUBLISH status + state hash"| Redis
    DS -->|"GEO rider pool; PUBLISH delivery, ETA, location"| Redis
    Redis -->|"Pub/Sub + state keys"| TS
    TS -.->|"fallback when a state key is missing"| OS
    TS -.->|fallback| RS

    US --> UserDB[(user_db)]
    RS --> RestDB[(restaurant_db)]
    OS --> OrderDB[(order_db)]
    DS --> DeliveryDB[(delivery_db)]
    DS --> Mongo[(MongoDB: rider_location_pings)]
    PS --> PaymentDB[(payment_db)]
    NS --> NotifDB[(notification_db)]

    OS -.->|outbox relay via Celery beat - Week 3| Kafka[(Kafka: order/delivery/payment-events)]
    DS -.->|outbox relay via Celery beat - Week 3| Kafka
    PS -.->|outbox relay via Celery beat - Week 3| Kafka
    Kafka -.-> NS
    Kafka -.-> AS[analytics-service - Week 3]
    NS -.->|dispatch tasks - Week 3| Rabbit[(RabbitMQ / Celery workers)]
```

Solid arrows are built; dotted arrows are Week 3 (per [PROGRESS.md](./PROGRESS.md)), the
tracking-service fallbacks (`/internal/*` calls made only when a Redis state key is
missing), or, for the `api-gateway` replica line, production-only (Kubernetes
`Deployment`/`HPA` + `Service` — never built locally, see
[technical-documentation.md](./technical-documentation.md#gateway-single-entry-point-not-a-spof)
for why). Each service's Temporal worker runs inside the service process in development
and as a separate process (`python -m app.worker`) in production. Live tracking goes
through Redis Pub/Sub, not Kafka: it needs sub-second push to whoever is watching, not a
replayable log. Every database is a separate Postgres container — database-per-service,
no shared instance.

## Order lifecycle (sequence)

The happy path, as built. Every arrow into "Saga" from an endpoint is a Temporal signal;
every arrow out of it is an activity on that service's own task queue.

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    actor A as Restaurant admin
    actor R as Rider
    participant GW as api-gateway
    participant TS as tracking-service
    participant OS as order-service
    participant W as Saga (order-saga queue)
    participant RS as restaurant-service
    participant PS as payment-service
    participant DS as delivery-service

    A->>GW: GET /v1/tracking/restaurants/{id} (SSE)
    GW->>TS: stream
    C->>GW: POST /v1/orders (Idempotency-Key)
    GW->>OS: forward
    OS->>RS: POST /internal/restaurants/{id}/quote
    OS->>OS: commit order `created` + OrderPlaced outbox
    OS->>W: start workflow order-{id}
    OS-->>C: 201 `created`
    C->>GW: GET /v1/tracking/orders/{id} (SSE)
    GW->>TS: stream (snapshot, then live)
    W->>RS: reserve_order (stock + capacity, atomic)
    W->>RS: redeem_promotion (if a code was given)
    W->>OS: apply_pricing (tax, delivery fee)
    W->>PS: authorize (simulated card)
    W->>OS: transition → awaiting_restaurant_acceptance
    OS-)TS: Redis PUBLISH → customer `status`, restaurant `new_order`
    A->>GW: POST /v1/orders/{id}/restaurant-response {accept}
    GW->>OS: signal restaurant_response
    W->>OS: transition → confirmed
    W->>DS: create_delivery (ready_at = now + prep time)
    W->>DS: offer_to_nearest_rider (GEOSEARCH + atomic claim)
    R->>GW: GET /v1/riders/me/offer, POST …/accept
    GW->>DS: forward → signal rider_response
    W->>DS: transition_delivery → assigned, compute_eta
    W->>OS: transition → assigned (RiderAssigned)
    R->>GW: PATCH /v1/riders/me/location (repeated)
    DS-)TS: Redis PUBLISH → customer `location`
    R->>GW: POST …/picked-up → signal
    W->>DS: transition_delivery → picked_up
    W->>OS: transition → picked_up
    W->>RS: complete_reservation (capacity freed)
    R->>GW: POST …/delivered → signal
    W->>DS: transition_delivery → delivered
    W->>PS: capture (final settlement)
    W->>OS: transition → delivered
    W->>DS: release_rider (rider available again)
    TS-->>C: `status: delivered`, stream ends
```

## Rider assignment and reassignment

One offer at a time, nearest first. A rider who declines, lets the offer expire, or drops
out is never offered that order again.

```mermaid
sequenceDiagram
    autonumber
    participant W as Saga
    participant DS as delivery-service
    participant Redis
    actor R1 as Rider 1 (nearest)
    actor R2 as Rider 2

    W->>DS: offer_to_nearest_rider(exclude = [])
    DS->>Redis: GEOSEARCH riders:available, 5 km, nearest first
    DS->>DS: UPDATE riders SET status='busy' WHERE id=R1 AND status='available'
    DS-->>W: offer to R1 (expires in 60s)
    alt R1 declines, or 60s pass
        R1->>DS: POST …/decline → signal rider_response(accepted=false)
        W->>DS: release_rider(declined / expired) → R1 available, reassignment_count + 1
        W->>DS: offer_to_nearest_rider(exclude = [R1])
        DS-->>W: offer to R2
        R2->>DS: POST …/accept → signal
        W->>DS: transition_delivery → assigned
    else R1 accepts, then drops out before pickup
        R1->>DS: POST …/accept → signal
        W->>DS: transition_delivery → assigned
        R1->>DS: POST …/drop-out (or no location ping for 3 min → stale-rider detector)
        DS->>W: signal rider_dropped
        W->>DS: release_rider(dropped) → R1 offline, reassignment_count + 1
        W->>DS: offer_to_nearest_rider(exclude = [R1]) → R2 …
    end
    Note over W,DS: Nobody free → look again every 30s. After 5 offers or 10 min:<br/>compensate → order `cancelled` (no_rider_available)
```

## Cancellation and compensation

A customer (or system admin) can cancel until pickup. The saga undoes, in reverse order,
only what it actually did; every compensation is idempotent and retried until it succeeds.

```mermaid
sequenceDiagram
    autonumber
    actor C as Customer
    participant OS as order-service
    participant W as Saga
    participant DS as delivery-service
    participant PS as payment-service
    participant RS as restaurant-service

    C->>OS: POST /v1/orders/{id}/cancel
    OS->>OS: can_cancel(status)? else 409
    OS->>W: signal cancel(actor, reason)
    OS-->>C: 202
    opt a delivery was created
        W->>DS: cancel_delivery (frees the rider)
    end
    opt payment was authorized
        W->>PS: void
    end
    opt a promotion was redeemed
        W->>RS: release_promotion
    end
    W->>RS: release_reservation (stock and capacity back)
    W->>OS: transition → cancelled (status_reason = reason or cancelled_by_customer)
```

The same compensation runs on every failure before pickup: restaurant rejects or doesn't
answer in 5 min (`rejected`), out of stock / at capacity / card declined (`rejected`), no
rider (`cancelled`). After pickup nothing is undone — the remaining steps retry until they
succeed, and a problem becomes a refund.

## Post-delivery refund

```mermaid
sequenceDiagram
    autonumber
    actor Admin as System admin
    participant OS as order-service
    participant RW as OrderRefundWorkflow (refund-{order_id})
    participant PS as payment-service

    Admin->>OS: POST /v1/orders/{id}/refund {amount?, reason}
    OS->>OS: delivered? not refunded yet? amount ≤ paid? else 409 / 422
    OS->>RW: start
    OS-->>Admin: 202
    RW->>PS: refund(refund_id = workflow ID) → payments.refunded_amount, RefundProcessed outbox
    RW->>OS: record_refund → orders.refunded_amount, history row
```

## Entity-relationship diagrams (one per service database)

No cross-service foreign keys anywhere — a column referencing another service's entity is
annotated as an opaque reference (e.g. `"opaque -> user_db.users.id"`), not a real FK,
since that would violate the database-per-service boundary.

### `user_db`

```mermaid
erDiagram
    USERS ||--o{ REFRESH_TOKENS : has
    USERS ||--o{ ADDRESSES : has

    USERS {
        string id PK
        string email UK
        string hashed_password
        string role
        string full_name
        string phone_number UK
        boolean email_verified
        boolean phone_verified
        boolean is_blocked
        datetime created_at
        datetime updated_at
    }
    REFRESH_TOKENS {
        string id PK "lookup_id, see sfo_auth.tokens"
        string user_id "same DB, but not an enforced FK"
        string token_hash
        datetime expires_at
        datetime revoked_at
        datetime created_at
    }
    ADDRESSES {
        string id PK
        string user_id FK
        string label
        string line1
        string line2
        string city
        float lat
        float lng
        boolean is_default
        datetime created_at
    }
```

### `restaurant_db`

```mermaid
erDiagram
    RESTAURANTS ||--o{ MENU_CATEGORIES : has
    RESTAURANTS ||--o{ MENU_ITEMS : has
    MENU_CATEGORIES ||--o{ MENU_ITEMS : groups
    RESTAURANTS ||--o{ PROMOTIONS : issues
    RESTAURANTS ||--o{ RESTAURANT_STATUS_HISTORY : "audit trail"
    RESTAURANTS ||--o{ STOCK_RESERVATIONS : holds
    PROMOTIONS ||--o{ PROMOTION_REDEMPTIONS : "redeemed by"

    RESTAURANTS {
        string id PK
        string admin_user_id "opaque -> user_db.users.id"
        string name
        string address
        string city
        float lat
        float lng
        string contact_phone
        string cuisine_type
        string status "pending_approval | active | suspended"
        boolean is_accepting_orders
        int max_concurrent_orders
        int active_order_count "reserved, not yet picked up"
        int avg_prep_time_minutes
        datetime created_at
    }
    MENU_CATEGORIES {
        string id PK
        string restaurant_id FK
        string name
    }
    MENU_ITEMS {
        string id PK
        string restaurant_id FK
        string category_id FK
        string name
        string description
        decimal price
        boolean is_available
        int stock_count
        int version "optimistic lock"
        datetime created_at
    }
    PROMOTIONS {
        string id PK
        string restaurant_id FK
        string code "unique per restaurant, not globally"
        string discount_type
        decimal discount_value
        datetime valid_from
        datetime valid_until
        int max_uses
        int times_used
        boolean is_active
        datetime created_at
    }
    RESTAURANT_STATUS_HISTORY {
        string id PK
        string restaurant_id FK
        string from_status "null = onboarding"
        string to_status
        string actor_id "opaque -> user_db.users.id"
        string actor_role
        string reason
        datetime created_at
    }
    STOCK_RESERVATIONS {
        string order_id PK "opaque -> order_db.orders.id; makes reserve/release idempotent"
        string restaurant_id FK
        json items "menu_item_id + quantity taken"
        string status "reserved | released | consumed"
        datetime created_at
        datetime updated_at
    }
    PROMOTION_REDEMPTIONS {
        string order_id PK "opaque -> order_db.orders.id"
        string promotion_id FK
        string status "redeemed | released"
        datetime created_at
        datetime updated_at
    }
```

A release that arrives before its reserve leaves a `released` row (`restaurant_id` /
`promotion_id` null), so a late reserve is a no-op.

### `order_db`

```mermaid
erDiagram
    ORDERS ||--o{ ORDER_ITEMS : contains
    ORDERS ||--o{ ORDER_STATUS_HISTORY : "audit trail"

    ORDERS {
        string id PK
        int order_number UK "human-facing sequential ID"
        string customer_id "opaque -> user_db.users.id"
        string restaurant_id "opaque -> restaurant_db.restaurants.id"
        string restaurant_admin_id "snapshot from the quote: who may accept"
        float restaurant_lat "snapshot: pickup point, delivery fee"
        float restaurant_lng
        string status
        string delivery_address_text "snapshotted, not a live ref"
        float delivery_lat
        float delivery_lng
        string special_instructions
        decimal subtotal
        decimal tax_amount
        decimal delivery_fee
        decimal discount_amount
        string promo_code
        decimal total_amount
        string payment_method "card_simulated | card_simulated_decline"
        decimal refunded_amount
        datetime refunded_at
        string status_reason
        string idempotency_key "UK with customer_id"
        string request_hash "detects key reuse with a different body"
        int version "optimistic lock"
        datetime created_at
        datetime updated_at
        datetime confirmed_at
        datetime rejected_at
        datetime cancelled_at
        datetime assigned_at
        datetime picked_up_at
        datetime delivered_at
    }
    ORDER_ITEMS {
        string id PK
        string order_id FK
        string menu_item_id "opaque -> restaurant_db.menu_items.id"
        string item_name_snapshot "captured at order time"
        int quantity
        decimal unit_price "from restaurant-service, never the client"
        string special_instructions
    }
    ORDER_STATUS_HISTORY {
        string id PK
        string order_id FK
        string from_status "null = creation"
        string to_status
        string actor_id "null = system/saga"
        string actor_role
        string reason
        datetime created_at
    }
```

### `delivery_db`

```mermaid
erDiagram
    RIDERS ||--o{ DELIVERIES : "assigned to"
    RIDERS ||--o{ RIDER_VERIFICATION_HISTORY : "audit trail"
    DELIVERIES ||--o{ DELIVERY_OFFERS : "offered to"
    RIDERS ||--o{ DELIVERY_OFFERS : receives
    DELIVERIES ||--o{ DELIVERY_STATUS_HISTORY : "audit trail"

    RIDERS {
        string id PK
        string user_id UK "opaque -> user_db.users.id"
        string status "available | busy | offline"
        string verification_status "pending_approval | approved | suspended"
        string vehicle_type
        string license_plate
        float last_lat "written at most every 30s; live position is in Redis"
        float last_lng
        datetime last_location_at
        datetime created_at
    }
    DELIVERIES {
        string id PK
        string order_id UK "opaque -> order_db.orders.id"
        string customer_id "opaque -> user_db.users.id, for ownership checks"
        string rider_id FK "current offer or assignment"
        string status "pending_assignment | offered | assigned | picked_up | delivered | cancelled"
        float pickup_lat "snapshot at creation"
        float pickup_lng
        float dropoff_lat
        float dropoff_lng
        float distance_km
        datetime ready_at "food ready: now + restaurant prep time"
        datetime offer_expires_at
        datetime pickup_eta
        datetime dropoff_eta
        datetime assigned_at
        datetime picked_up_at
        datetime delivered_at
        datetime cancelled_at
        int reassignment_count
        int version "optimistic lock"
        datetime created_at
    }
    RIDER_VERIFICATION_HISTORY {
        string id PK
        string rider_id FK
        string from_status "null = registration"
        string to_status
        string actor_id
        string actor_role
        string reason
        datetime created_at
    }
    DELIVERY_OFFERS {
        string id PK
        string delivery_id FK
        string rider_id FK
        string status "pending | accepted | declined | expired | dropped | cancelled | completed"
        float distance_km "rider to pickup"
        datetime offered_at
        datetime expires_at
        datetime responded_at
    }
    DELIVERY_STATUS_HISTORY {
        string id PK
        string delivery_id FK
        string from_status "null = created"
        string to_status
        string rider_id
        string reason
        datetime created_at
    }
```

High-frequency rider location pings live in MongoDB, not this diagram —
the live position and the assignment pool are in Redis (`rider:{id}:location`, GEO set
`riders:available`), and `riders.last_lat/last_lng` is a throttled copy:

```mermaid
erDiagram
    RIDER_LOCATION_PINGS {
        string _id PK "sfo_common.new_id(), not a Mongo ObjectId"
        string rider_id "opaque -> delivery_db.riders.id"
        float lat
        float lng
        datetime recorded_at "TTL index, expires after 24h"
    }
```

### `payment_db`

```mermaid
erDiagram
    PAYMENTS ||--o{ REFUNDS : "refunded by"

    PAYMENTS {
        string id PK
        string order_id UK "opaque -> order_db.orders.id"
        string customer_id "opaque -> user_db.users.id, for ownership checks"
        decimal amount
        string status "authorized | captured | voided | refunded | declined"
        string payment_method
        string failure_reason
        decimal refunded_amount "sum of refunds, never above amount"
        string idempotency_key UK "= order_id: one payment per order"
        datetime created_at
        datetime authorized_at
        datetime captured_at
        datetime voided_at
        datetime refunded_at
    }
    REFUNDS {
        string id PK "refund workflow ID: a retried refund is a no-op"
        string payment_id FK
        decimal amount
        string reason
        datetime created_at
    }
```

### `notification_db`

```mermaid
erDiagram
    NOTIFICATIONS {
        string id PK
        string user_id "opaque -> user_db.users.id"
        string related_order_id "opaque -> order_db.orders.id"
        string type
        string channel "in_app | email | sms | push"
        string content
        string status "pending | sent | failed"
        int retry_count
        string last_error
        datetime created_at
        datetime sent_at
        datetime read_at
    }
```

### `outbox_events` (shared shape, present in `restaurant_db`/`order_db`/`delivery_db`/`payment_db`)

```mermaid
erDiagram
    OUTBOX_EVENTS {
        string id PK
        string aggregate_id "Kafka partition key"
        string event_type
        json payload
        string correlation_id
        datetime created_at
        datetime dispatched_at "null until relayed - Week 3"
    }
```
