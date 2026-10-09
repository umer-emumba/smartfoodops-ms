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
    GW1 -->|deny-list check + rate limit| Redis[(Redis)]
    US -->|deny-list write on block| Redis
    RS -->|public menu/detail cache| Redis

    OS -->|sync REST: live prices + availability at order creation| RS
    OS -.->|Temporal saga activities - Week 2| PS
    OS -.->|Temporal saga activities - Week 2| DS
    OS -.->|saga worker - Week 2| Temporal[(Temporal server)]

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
    Kafka -.->|DeliveryStatusUpdated → customer WS/SSE - Week 2/3| GW1
    Kafka -.-> NS
    Kafka -.-> AS[analytics-service - Week 3]
    NS -.->|dispatch tasks - Week 3| Rabbit[(RabbitMQ / Celery workers)]
```

Solid arrows are built (Week 1); dotted arrows are either Week 2/3 (per
[PROGRESS.md](./PROGRESS.md)) or, for the `api-gateway` replica line specifically,
production-only (Kubernetes `Deployment`/`HPA` + `Service` — never built locally, see
[technical-documentation.md](./technical-documentation.md#gateway-single-entry-point-not-a-spof)
for why). `restaurant-service` has no Kafka topic: nothing downstream consumes restaurant
changes in Part A. Every database is a separate Postgres container — database-per-service, no
shared instance.

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
```

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
        string status_reason
        string idempotency_key "UK with customer_id"
        string request_hash "detects key reuse with a different body"
        int version "optimistic lock"
        datetime created_at
        datetime updated_at
        datetime confirmed_at
        datetime rejected_at
        datetime cancelled_at
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

    RIDERS {
        string id PK
        string user_id UK "opaque -> user_db.users.id"
        string status "available | busy | offline"
        string verification_status "pending_approval | approved | suspended"
        string vehicle_type
        string license_plate
        float last_lat
        float last_lng
        datetime created_at
    }
    DELIVERIES {
        string id PK
        string order_id UK "opaque -> order_db.orders.id"
        string rider_id FK
        string status "pending_assignment | offered | assigned | picked_up | delivered | cancelled (Week 2)"
        float pickup_lat "snapshot at assignment"
        float pickup_lng
        float dropoff_lat
        float dropoff_lng
        float distance_km
        datetime pickup_eta
        datetime assigned_at
        datetime picked_up_at
        datetime delivered_at
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
```

High-frequency rider location pings live in MongoDB, not this diagram —
`riders.last_lat/last_lng` is only the latest known position used for assignment queries:

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
    PAYMENTS {
        string id PK
        string order_id UK "opaque -> order_db.orders.id"
        string customer_id "opaque -> user_db.users.id, for ownership checks"
        decimal amount
        string status "authorized | captured | refunded | failed"
        string payment_method
        string failure_reason
        decimal refunded_amount
        string idempotency_key UK
        datetime created_at
        datetime authorized_at
        datetime refunded_at
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
