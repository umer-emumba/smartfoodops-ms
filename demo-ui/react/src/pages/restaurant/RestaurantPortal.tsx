// The restaurant admin's portal: pick one of their restaurants (or create
// the first), then a tab per area.
import { Badge, Button, Card, Group, NumberInput, Select, Stack, Switch, Tabs, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { api, get } from "../../api";
import { run, useLoad, usePaged } from "../../hooks";
import type { Order, Restaurant } from "../../types";
import { Empty, LoadMore, StatusBadge } from "../../ui";
import Analytics from "../Analytics";
import OrderTable from "../OrderTable";
import LiveOrders from "./LiveOrders";
import MenuManager from "./MenuManager";
import Promotions from "./Promotions";

export default function RestaurantPortal() {
  const { data: mine, setData, reload } = useLoad(() => get<Restaurant[]>("/v1/restaurants/mine"), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  if (!mine) return <Empty>Loading…</Empty>;
  if (mine.length === 0) return <RestaurantForm onSaved={reload} />;
  const restaurant = mine.find((r) => r.id === selectedId) ?? mine[0];
  const replace = (updated: Restaurant) => setData(mine.map((r) => (r.id === updated.id ? updated : r)));

  return (
    <>
      <Group justify="space-between" mb="md">
        <Group>
          <Title order={3}>{restaurant.name}</Title>
          <StatusBadge status={restaurant.status} />
          <Badge color={restaurant.is_accepting_orders ? "green" : "gray"} variant="outline">
            {restaurant.is_accepting_orders ? "accepting orders" : "not accepting"}
          </Badge>
        </Group>
        {mine.length > 1 && (
          <Select
            value={restaurant.id}
            onChange={setSelectedId}
            data={mine.map((r) => ({ value: r.id, label: r.name }))}
          />
        )}
      </Group>
      {restaurant.status !== "active" && (
        <Card withBorder bg="yellow.0" mb="md">
          This restaurant is {restaurant.status.replaceAll("_", " ")}: customers can't see it until an admin approves it.
        </Card>
      )}
      <Tabs defaultValue="live" keepMounted={false}>
        <Tabs.List mb="md">
          <Tabs.Tab value="live">Live orders</Tabs.Tab>
          <Tabs.Tab value="orders">All orders</Tabs.Tab>
          <Tabs.Tab value="menu">Menu</Tabs.Tab>
          <Tabs.Tab value="promotions">Promotions</Tabs.Tab>
          <Tabs.Tab value="analytics">Analytics</Tabs.Tab>
          <Tabs.Tab value="settings">Settings</Tabs.Tab>
        </Tabs.List>
        <Tabs.Panel value="live">
          <LiveOrders key={restaurant.id} restaurant={restaurant} />
        </Tabs.Panel>
        <Tabs.Panel value="orders">
          <RestaurantOrders restaurantId={restaurant.id} />
        </Tabs.Panel>
        <Tabs.Panel value="menu">
          <MenuManager restaurantId={restaurant.id} />
        </Tabs.Panel>
        <Tabs.Panel value="promotions">
          <Promotions restaurantId={restaurant.id} />
        </Tabs.Panel>
        <Tabs.Panel value="analytics">
          <Analytics path={`/v1/analytics/restaurants/${restaurant.id}`} />
        </Tabs.Panel>
        <Tabs.Panel value="settings">
          <RestaurantForm restaurant={restaurant} onSaved={replace} />
        </Tabs.Panel>
      </Tabs>
    </>
  );
}

const STATUSES = ["awaiting_restaurant_acceptance", "confirmed", "assigned", "picked_up", "delivered", "rejected", "cancelled"];

function RestaurantOrders({ restaurantId }: { restaurantId: string }) {
  const [status, setStatus] = useState<string | null>(null);
  const list = usePaged<Order>(`/v1/orders/restaurant/${restaurantId}${status ? `?status=${status}` : ""}`);
  return (
    <Card withBorder>
      <Group mb="sm">
        <Select
          placeholder="Any status"
          clearable
          value={status}
          onChange={setStatus}
          data={STATUSES.map((s) => ({ value: s, label: s.replaceAll("_", " ") }))}
        />
        <Button variant="default" onClick={list.reload}>
          Refresh
        </Button>
      </Group>
      {list.items.length === 0 ? <Empty>{list.loading ? "Loading…" : "No orders"}</Empty> : <OrderTable orders={list.items} />}
      <LoadMore {...list} />
    </Card>
  );
}

// Create a restaurant (no `restaurant`), or edit one.
function RestaurantForm({ restaurant, onSaved }: { restaurant?: Restaurant; onSaved: (r: Restaurant) => void }) {
  const [form, setForm] = useState({
    name: restaurant?.name ?? "",
    address: restaurant?.address ?? "",
    city: restaurant?.city ?? "Karachi",
    lat: restaurant?.lat ?? 24.8138,
    lng: restaurant?.lng ?? 67.03,
    contact_phone: restaurant?.contact_phone ?? "",
    cuisine_type: restaurant?.cuisine_type ?? "",
    is_accepting_orders: restaurant?.is_accepting_orders ?? true,
    max_concurrent_orders: restaurant?.max_concurrent_orders ?? 10,
    avg_prep_time_minutes: restaurant?.avg_prep_time_minutes ?? 20,
  });
  const set = (patch: Partial<typeof form>) => setForm({ ...form, ...patch });

  const save = async () => {
    const { is_accepting_orders, max_concurrent_orders, avg_prep_time_minutes, city, ...base } = form;
    const saved = restaurant
      ? await run(
          () =>
            api<Restaurant>("PATCH", `/v1/restaurants/${restaurant.id}`, {
              ...base,
              cuisine_type: base.cuisine_type || null,
              is_accepting_orders,
              max_concurrent_orders,
              avg_prep_time_minutes,
            }),
          "Saved",
        )
      : await run(
          () => api<Restaurant>("POST", "/v1/restaurants", { ...base, city, cuisine_type: base.cuisine_type || null }),
          "Restaurant created — waiting for admin approval",
        );
    if (saved) onSaved(saved);
  };

  return (
    <Card withBorder maw={560}>
      <Title order={4} mb="sm">
        {restaurant ? "Settings" : "Create your restaurant"}
      </Title>
      <Stack gap="xs">
        <TextInput label="Name" value={form.name} onChange={(e) => set({ name: e.currentTarget.value })} />
        <TextInput label="Address" value={form.address} onChange={(e) => set({ address: e.currentTarget.value })} />
        <TextInput label="City" disabled={!!restaurant} value={form.city} onChange={(e) => set({ city: e.currentTarget.value })} />
        <Group grow>
          <NumberInput label="Lat" decimalScale={5} value={form.lat} onChange={(v) => set({ lat: Number(v) })} />
          <NumberInput label="Lng" decimalScale={5} value={form.lng} onChange={(v) => set({ lng: Number(v) })} />
        </Group>
        <TextInput label="Contact phone" value={form.contact_phone} onChange={(e) => set({ contact_phone: e.currentTarget.value })} />
        <TextInput label="Cuisine" value={form.cuisine_type} onChange={(e) => set({ cuisine_type: e.currentTarget.value })} />
        {restaurant && (
          <>
            <Switch
              label="Accepting orders"
              checked={form.is_accepting_orders}
              onChange={(e) => set({ is_accepting_orders: e.currentTarget.checked })}
            />
            <Group grow>
              <NumberInput
                label="Max concurrent orders"
                min={1}
                value={form.max_concurrent_orders}
                onChange={(v) => set({ max_concurrent_orders: Number(v) })}
              />
              <NumberInput
                label="Avg prep time (min)"
                min={1}
                value={form.avg_prep_time_minutes}
                onChange={(v) => set({ avg_prep_time_minutes: Number(v) })}
              />
            </Group>
          </>
        )}
        <Group>
          <Button onClick={save}>{restaurant ? "Save" : "Create"}</Button>
        </Group>
      </Stack>
    </Card>
  );
}
