// Rider panel (used twice): go on shift near the restaurant, answer offers,
// move, pick up, deliver, or drop out. Offers are pushed on the rider's live
// feed (/v1/tracking/riders/me); each event — and each (re)connect's
// snapshot — is a nudge to re-read the offer or held delivery over REST.
// A slow safety poll covers the feed being down.
import { api, find } from "./api.js";
import { openFeed } from "./sse.js";
import { $, attempt, button, logger, north } from "./ui.js";

const SAFETY_POLL_MS = 30000;

export function createRider(root, who, title, defaultKm) {
  root.append($("#rider-template").content.cloneNode(true));
  $(".r-title", root).textContent = title;
  $(".r-km", root).value = defaultKm;
  const log = logger($(".r-log", root));
  let restaurant = null;
  let me = null;
  let offer = null; // open offer
  let delivery = null; // accepted: {id, order_id, pickup, dropoff, status}
  let closeFeed = () => {};

  async function refreshMe() {
    me = await attempt(log, () => api(who, "GET", "/v1/riders/me"));
    if (me) $(".r-status", root).textContent = `Status: ${me.status} (${me.vehicle_type}, ${me.verification_status})`;
  }

  const move = (point, label) =>
    attempt(log, async () => {
      await api(who, "PATCH", "/v1/riders/me/location", point);
      log(`Moved ${label}`);
    });

  async function setStatus(status) {
    const ok = await attempt(log, () => api(who, "PATCH", "/v1/riders/me/status", { status }));
    if (ok && status === "available") await move(north(restaurant, Number($(".r-km", root).value)), `${$(".r-km", root).value} km from the restaurant`);
    await refreshMe();
  }

  async function act(action, body) {
    const id = offer?.delivery_id ?? delivery?.id;
    const ok = await attempt(log, () => api(who, "POST", `/v1/riders/me/deliveries/${id}/${action}`, body));
    if (!ok) return;
    log(`Sent: ${action}`);
    if (action === "accept") {
      delivery = {
        id: offer.delivery_id,
        order_id: offer.order_id,
        pickup: { lat: offer.pickup_lat, lng: offer.pickup_lng },
        dropoff: { lat: offer.dropoff_lat, lng: offer.dropoff_lng },
        status: "offered",
      };
    }
    offer = null;
    render();
  }

  // The offer or held delivery, re-read over REST (the source of truth).
  async function refresh() {
    if (!me) return;
    if (delivery) {
      const current = await find(who, `/v1/deliveries/by-order/${delivery.order_id}`).catch(() => null);
      if (!current || current.rider_id !== me.id || ["delivered", "cancelled"].includes(current.status)) {
        log(`Delivery ended: ${current?.rider_id === me.id ? current.status : "no longer mine"}`);
        delivery = null;
        await refreshMe();
      } else {
        delivery.status = current.status;
      }
    } else {
      const next = await find(who, "/v1/riders/me/offer").catch(() => null);
      if (next && next.delivery_id !== offer?.delivery_id) log(`New offer: ${next.distance_to_pickup_km} km to pickup`);
      offer = next;
    }
    render();
  }

  function render() {
    const offerBox = $(".r-offer", root);
    offerBox.replaceChildren();
    if (offer) {
      const left = Math.max(0, Math.round((new Date(offer.expires_at) - Date.now()) / 1000));
      offerBox.append(
        `Offer: ${offer.distance_to_pickup_km} km to pickup, ${offer.delivery_distance_km.toFixed(1)} km delivery, ${left}s left `,
        button("Accept", () => act("accept")),
        " ",
        button("Decline", () => act("decline")),
      );
    }
    const deliveryBox = $(".r-delivery", root);
    deliveryBox.replaceChildren();
    if (delivery) {
      deliveryBox.append(
        `Delivery: ${delivery.status} `,
        button("Ride to restaurant", () => move(delivery.pickup, "to the restaurant")),
        button("Picked up", () => act("picked-up")),
        button("Ride to customer", () => move(delivery.dropoff, "to the customer")),
        button("Delivered", () => act("delivered")),
        button("Drop out", () => act("drop-out", { reason: "Can't complete" })),
      );
    }
  }

  function openRiderFeed() {
    closeFeed();
    closeFeed = openFeed(
      who,
      "/v1/tracking/riders/me",
      {
        snapshot: () => {
          log("Live feed connected", "event");
          refresh();
        },
        offer: () => {
          log("Offer pushed", "event");
          refresh();
        },
        released: (e) => {
          log(`Released: ${e.reason}`, "event");
          refresh();
        },
        delivery: (e) => {
          log(`Delivery ${e.status}`, "event");
          refresh();
        },
      },
      () => log("Live feed closed; checking every 30s instead"),
    );
  }

  $(".r-shift", root).onclick = () => setStatus("available");
  $(".r-offline", root).onclick = () => setStatus("offline");
  setInterval(refresh, SAFETY_POLL_MS); // does nothing until logged in
  setInterval(() => offer && render(), 1000); // the offer's countdown; no requests

  return {
    async start() {
      // after logging in
      await refreshMe();
      openRiderFeed();
    },
    setRestaurant(selected) {
      restaurant = selected;
    },
  };
}
