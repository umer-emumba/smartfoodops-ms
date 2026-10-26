// Restaurant: live feed of orders awaiting a decision; accept or reject.
import { api } from "./api.js";
import { openFeed } from "./sse.js";
import { $, attempt, button, logger } from "./ui.js";

const log = logger($("#r-log"));
const pending = new Map(); // order_id -> {order_number, total_amount}
let closeFeed = () => {};

export function setRestaurant(restaurant) {
  closeFeed();
  pending.clear();
  render();
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
        if (pending.delete(e.order_id)) log(`Order #${e.order_number} was cancelled`, "event");
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
  log(`#${pending.get(orderId)?.order_number}: ${decision}ed`);
  pending.delete(orderId);
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
}
