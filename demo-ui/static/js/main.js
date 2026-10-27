// Wires the panels together: log in, pick a restaurant, hand it to every panel.
import "./admin.js";
import { loginAll } from "./accounts.js";
import { api } from "./api.js";
import * as customer from "./customer.js";
import * as restaurantPanel from "./restaurant.js";
import { createRider } from "./rider.js";
import { $ } from "./ui.js";

const riders = [
  createRider($("#rider1"), "rider1", "Rider 1", 0.3),
  createRider($("#rider2"), "rider2", "Rider 2", 1.0),
];
let restaurants = [];
const status = (text) => ($("#header-status").textContent = text);

async function selectRestaurant(id) {
  const selected = restaurants.find((r) => r.id === id);
  await customer.setRestaurant(selected);
  restaurantPanel.setRestaurant(selected);
  for (const rider of riders) rider.setRestaurant(selected);
}

async function start() {
  try {
    status("Logging in…");
    await loginAll();
    const mine = await api("owner", "GET", "/v1/restaurants/mine");
    restaurants = mine.filter((r) => r.status === "active");
    if (!restaurants.length) throw new Error("No active restaurant — run scripts/seed.py");
    $("#restaurant").replaceChildren(...restaurants.map((r, i) => new Option(`${r.name} #${i + 1}`, r.id)));
    for (const rider of riders) await rider.start();
    await selectRestaurant(restaurants[0].id);
    status("Logged in as all demo accounts");
  } catch (error) {
    status(error.message);
  }
}

$("#login-all").onclick = start;
$("#restaurant").onchange = (e) => selectRestaurant(e.target.value);
