// Restaurant: live feed of orders awaiting a decision; accept or reject.
// Accepted orders are listed as preparing until their food is marked ready.
import { api } from "./api.js";
import { openFeed } from "./sse.js";
import { $, attempt, button, logger } from "./ui.js";

const log = logger($("#r-log"));
const pending = new Map(); // order_id -> {order_number, total_amount}
const preparing = new Map(); // order_id -> {order_number}
let closeFeed = () => {};

export function setRestaurant(restaurant) {
  closeFeed();
  pending.clear();
  preparing.clear();
  render();
  loadPreparing(restaurant);
  closeFeed = openFeed(
    "owner",
    `/v1/tracking/restaurants/${restaurant.id}`,
    {
      snapshot: (s) => {
        for (const order of s.pending) pending.set(order.order_id, order);
        log(`Connected to ${restaurant.name}: ${s.pending.length} waiting`, "event");
        render();
      },
      new_order: (e) => {
        pending.set(e.order_id, e);
        log(`New order #${e.order_number} (${e.total_amount})`, "event");
        render();
      },
      order_cancelled: (e) => {
        if (pending.delete(e.order_id) || preparing.delete(e.order_id)) {
          log(`Order #${e.order_number} was cancelled`, "event");
        }
        render();
      },
    },
    () => log("Live feed closed — pick the restaurant again to reconnect", "error"),
  );
}

async function respond(orderId, decision) {
  const body = decision === "reject" ? { decision, reason: "Kitchen busy" } : { decision };
  const result = await attempt(log, () =>
    api("owner", "POST", `/v1/orders/${orderId}/restaurant-response`, body),
  );
  if (!result) return;
  const order = pending.get(orderId);
  log(`#${order?.order_number}: ${decision}ed`);
  pending.delete(orderId);
  if (decision === "accept" && order) preparing.set(orderId, order);
  render();
}

// Confirmed orders the kitchen is working on (the feed only carries new ones).
// Orders marked ready before a page reload show again; marking twice is harmless.
async function loadPreparing(restaurant) {
  for (const status of ["confirmed", "assigned"]) {
    const page = await attempt(log, () =>
      api("owner", "GET", `/v1/orders/restaurant/${restaurant.id}?status=${status}`),
    );
    for (const order of page?.items ?? []) preparing.set(order.id, order);
  }
  render();
}

async function markReady(orderId) {
  const result = await attempt(log, () => api("owner", "POST", `/v1/orders/${orderId}/food-ready`));
  if (!result) return;
  log(`#${preparing.get(orderId)?.order_number}: food ready`);
  preparing.delete(orderId);
  render();
}

function render() {
  const rows = [...pending.entries()].map(([id, order]) => {
    const actions = document.createElement("span");
    actions.append(button("Accept", () => respond(id, "accept")), " ", button("Reject", () => respond(id, "reject")));
    const row = document.createElement("div");
    row.className = "pending";
    row.append(`#${order.order_number} · ${order.total_amount}`, actions);
    return row;
  });
  $("#r-pending").replaceChildren(...(rows.length ? rows : ["No orders waiting"]));
  const cooking = [...preparing.entries()].map(([id, order]) => {
    const row = document.createElement("div");
    row.className = "pending";
    row.append(`#${order.order_number}`, button("Mark ready", () => markReady(id)));
    return row;
  });
  $("#r-preparing").replaceChildren(...(cooking.length ? cooking : ["Nothing being prepared"]));
}
