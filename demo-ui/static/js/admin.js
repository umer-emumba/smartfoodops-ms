// Admin: refund the customer's current order (delivered orders only), or
// mark it delivered when the rider never did (picked-up orders only).
import { api } from "./api.js";
import { currentOrderId, refreshDetails } from "./customer.js";
import { $, attempt, logger } from "./ui.js";

const log = logger($("#a-log"));

async function refund() {
  const orderId = currentOrderId();
  if (!orderId) return log("Place an order first", "error");
  const amount = $("#a-amount").value.trim();
  const body = { reason: $("#a-reason").value || "Refund", ...(amount ? { amount } : {}) };
  const order = await attempt(log, () => api("admin", "POST", `/v1/orders/${orderId}/refund`, body));
  if (!order) return;
  log(`Refund of ${amount || "the full amount"} requested`);
  setTimeout(refreshDetails, 1500); // the refund workflow takes a moment
}

async function markDelivered() {
  const orderId = currentOrderId();
  if (!orderId) return log("Place an order first", "error");
  const order = await attempt(log, () => api("admin", "POST", `/v1/orders/${orderId}/mark-delivered`));
  if (order) log("Marked delivered");
}

$("#a-refund").onclick = refund;
$("#a-mark-delivered").onclick = markDelivered;
