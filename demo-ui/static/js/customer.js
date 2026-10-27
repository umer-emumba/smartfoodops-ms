// Customer: place an order, follow it live, cancel it, see payment and delivery.
import { api, find } from "./api.js";
import { TEMPORAL_UI } from "./config.js";
import { openFeed } from "./sse.js";
import { $, attempt, logger, table, time } from "./ui.js";

const FINISHED = ["delivered", "rejected", "cancelled"];
const log = logger($("#c-log"));
let restaurant = null;
let orderId = null;
let closeFeed = () => {};

export const currentOrderId = () => orderId;

export async function setRestaurant(selected) {
  restaurant = selected;
  $("#c-lat").value = (selected.lat + 0.02).toFixed(4);
  $("#c-lng").value = (selected.lng + 0.01).toFixed(4);
  await loadMenu();
}

async function loadMenu() {
  const chosen = $("#c-item").value;
  const menu = await attempt(log, () => api("customer", "GET", `/v1/restaurants/${restaurant.id}/menu`));
  $("#c-item").replaceChildren(
    ...(menu ?? []).map((item) => {
      const option = new Option(`${item.name} — ${item.price} (stock ${item.stock_count ?? "∞"})`, item.id);
      option.disabled = !item.is_available;
      return option;
    }),
  );
  if (chosen) $("#c-item").value = chosen;
}

async function place() {
  const order = await attempt(log, () =>
    api(
      "customer",
      "POST",
      "/v1/orders",
      {
        restaurant_id: restaurant.id,
        delivery_address_text: "Demo drop-off",
        delivery_lat: Number($("#c-lat").value),
        delivery_lng: Number($("#c-lng").value),
        items: [{ menu_item_id: $("#c-item").value, quantity: Number($("#c-qty").value) }],
        payment_method: $("#c-payment").value,
      },
      { "Idempotency-Key": crypto.randomUUID() },
    ),
  );
  if (!order) return;
  closeFeed();
  orderId = order.id;
  $("#c-timeline").replaceChildren();
  $("#c-order").classList.remove("muted");
  const workflow = document.createElement("a");
  workflow.href = `${TEMPORAL_UI}/namespaces/default/workflows/order-${encodeURIComponent(order.id)}`;
  workflow.target = "_blank";
  workflow.rel = "noopener";
  workflow.textContent = "workflow";
  $("#c-order").replaceChildren(`Order #${order.order_number} · `, workflow);
  log(`Placed order #${order.order_number} (${order.status})`);
  follow(order.id);
  await refreshDetails();
}

// Live tracking: the stream ends by itself once the order is finished.
function follow(id) {
  const onStatus = (status, reason) => {
    addChip(status);
    if (FINISHED.includes(status)) {
      closeFeed();
      log(`Finished: ${status}${reason ? ` (${reason})` : ""}`);
      loadMenu(); // stock changed
    }
    refreshDetails();
  };
  closeFeed = openFeed(
    "customer",
    `/v1/tracking/orders/${id}`,
    {
      snapshot: (s) => {
        log(`snapshot: ${s.status}`, "event");
        onStatus(s.status, s.status_reason);
      },
      status: (e) => {
        log(`status: ${e.status}${e.reason ? ` (${e.reason})` : ""}`, "event");
        onStatus(e.status, e.reason);
      },
      delivery: (e) => {
        const eta = e.pickup_eta ? `, pickup ETA ${time(e.pickup_eta)}, drop-off ETA ${time(e.dropoff_eta)}` : "";
        const ready = e.food_ready_at ? " — food ready" : "";
        log(`delivery: ${e.status}${ready}${eta}`, "event");
        refreshDetails();
      },
      location: (e) => log(`rider at ${e.lat.toFixed(4)}, ${e.lng.toFixed(4)}`, "event"),
    },
    () => log("Live feed closed", "error"),
  );
}

function addChip(status) {
  const chips = $("#c-timeline");
  if (chips.lastChild?.textContent === status) return;
  const chip = document.createElement("span");
  chip.className = `chip ${status === "delivered" ? "good" : ["rejected", "cancelled"].includes(status) ? "bad" : ""}`;
  chip.textContent = status;
  chips.append(chip);
}

export async function refreshDetails() {
  if (!orderId) return;
  const [order, payment, delivery] = await Promise.all([
    find("customer", `/v1/orders/${orderId}`),
    find("customer", `/v1/payments/by-order/${orderId}`),
    find("customer", `/v1/deliveries/by-order/${orderId}`),
  ]).catch((error) => (log(error.message, "error"), []));
  if (!order) return;
  $("#c-details").replaceChildren(
    table([
      ["Order", `${order.status}${order.status_reason ? ` (${order.status_reason})` : ""}`],
      ["Total", `${order.subtotal} + tax ${order.tax_amount} + fee ${order.delivery_fee} = ${order.total_amount}`],
      ["Refunded", order.refunded_amount || null],
      ["Payment", payment && `${payment.status}${payment.failure_reason ? ` (${payment.failure_reason})` : ""}`],
      ["Delivery", delivery && `${delivery.status}, reassigned ${delivery.reassignment_count}×`],
      ["Food ready", time(delivery?.food_ready_at)],
      ["Pickup ETA", time(delivery?.pickup_eta)],
      ["Drop-off ETA", time(delivery?.dropoff_eta)],
    ]),
  );
}

async function cancel() {
  if (!orderId) return;
  const order = await attempt(log, () => api("customer", "POST", `/v1/orders/${orderId}/cancel`, {}));
  if (order) log("Cancel requested");
}

$("#c-place").onclick = place;
$("#c-cancel").onclick = cancel;
