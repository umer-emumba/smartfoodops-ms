// Same origin: the Vite dev server proxies /v1 to the gateway (vite.config.ts).
export const GATEWAY = "";
export const TEMPORAL_UI = "http://localhost:8088";

// Seeded by scripts/seed.py; dummy local values.
export const DEMO_PASSWORD = "DemoPass123!";
export const DEMO_ACCOUNTS = [
  { label: "Customer", email: "customer@demo.smartfoodops.example" },
  { label: "Restaurant", email: "restaurant-admin@demo.smartfoodops.example" },
  { label: "Rider 1", email: "rider@demo.smartfoodops.example" },
  { label: "Rider 2", email: "rider2@demo.smartfoodops.example" },
  { label: "Admin", email: "admin@demo.smartfoodops.example" },
];

export const workflowUrl = (orderId: string) =>
  `${TEMPORAL_UI}/namespaces/default/workflows/order-${encodeURIComponent(orderId)}`;
