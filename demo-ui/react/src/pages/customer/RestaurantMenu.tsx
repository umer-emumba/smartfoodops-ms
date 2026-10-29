// A restaurant's menu, a cart, and checkout: drop-off address (saved or
// typed coordinates), payment method, promo code.
import {
  ActionIcon,
  Badge,
  Button,
  Card,
  Grid,
  Group,
  NumberInput,
  Select,
  Stack,
  Text,
  Textarea,
  TextInput,
  Title,
} from "@mantine/core";
import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { api, find, get } from "../../api";
import { run, useLoad } from "../../hooks";
import type { Address, MenuCategory, MenuItem, Order, Promotion, Restaurant } from "../../types";
import { Empty, money } from "../../ui";

const CUSTOM = "custom";

export default function RestaurantMenu() {
  const { id } = useParams() as { id: string };
  const navigate = useNavigate();
  const { data, setData } = useLoad(
    async () => {
      const [restaurant, menu, categories, promotions, addresses] = await Promise.all([
        get<Restaurant>(`/v1/restaurants/${id}`),
        get<MenuItem[]>(`/v1/restaurants/${id}/menu`),
        get<MenuCategory[]>(`/v1/restaurants/${id}/menu/categories`),
        find<Promotion[]>(`/v1/restaurants/${id}/promotions`),
        get<Address[]>("/v1/users/me/addresses"),
      ]);
      return { restaurant, menu, categories, promotions: promotions ?? [], addresses };
    },
    [id],
  );
  const [cart, setCart] = useState<Record<string, number>>({});
  const [addressId, setAddressId] = useState<string | null>(null);
  const [dropoff, setDropoff] = useState({ text: "Demo drop-off", lat: "", lng: "" });
  const [payment, setPayment] = useState("card_simulated");
  const [promo, setPromo] = useState("");
  const [instructions, setInstructions] = useState("");
  const [placing, setPlacing] = useState(false);

  if (!data) return <Empty>Loading…</Empty>;
  const { restaurant, menu, categories, promotions, addresses } = data;
  const chosenAddress = addressId ?? addresses.find((a) => a.is_default)?.id ?? addresses[0]?.id ?? CUSTOM;
  const lines = menu.filter((item) => cart[item.id]);
  const subtotal = lines.reduce((sum, item) => sum + item.price * cart[item.id], 0);

  const setQty = (itemId: string, qty: number) =>
    setCart(({ [itemId]: _, ...rest }) => (qty > 0 ? { ...rest, [itemId]: qty } : rest));

  const place = async () => {
    const address = addresses.find((a) => a.id === chosenAddress);
    // A typed drop-off defaults to ~2 km from the restaurant.
    const target = address
      ? { text: [address.line1, address.city].join(", "), lat: address.lat, lng: address.lng }
      : {
          text: dropoff.text,
          lat: dropoff.lat ? Number(dropoff.lat) : restaurant.lat + 0.02,
          lng: dropoff.lng ? Number(dropoff.lng) : restaurant.lng + 0.01,
        };
    setPlacing(true);
    const order = await run(() =>
      api<Order>(
        "POST",
        "/v1/orders",
        {
          restaurant_id: restaurant.id,
          delivery_address_text: target.text,
          delivery_lat: target.lat,
          delivery_lng: target.lng,
          special_instructions: instructions || null,
          promo_code: promo || null,
          items: lines.map((item) => ({ menu_item_id: item.id, quantity: cart[item.id] })),
          payment_method: payment,
        },
        { "Idempotency-Key": crypto.randomUUID() },
      ),
    );
    setPlacing(false);
    if (order) navigate(`/orders/${order.id}`);
  };

  return (
    <Grid>
      <Grid.Col span={{ base: 12, md: 8 }}>
        <Title order={3}>{restaurant.name}</Title>
        <Text c="dimmed" mb="md">
          {restaurant.address} · {restaurant.is_accepting_orders ? "accepting orders" : "not accepting orders"}
        </Text>
        {promotions.length > 0 && (
          <Group mb="md">
            {promotions.map((p) => (
              <Badge key={p.id} variant="outline" color="grape">
                {p.code}: {p.discount_type === "percentage" ? `${p.discount_value}%` : money(p.discount_value)} off
              </Badge>
            ))}
          </Group>
        )}
        {menu.length === 0 && <Empty>No menu items</Empty>}
        {categories.map((category) => {
          const items = menu.filter((item) => item.category_id === category.id);
          if (items.length === 0) return null;
          return (
            <Stack key={category.id} gap="xs" mb="md">
              <Text fw={600}>{category.name}</Text>
              {items.map((item) => (
                <Card key={item.id} withBorder padding="sm">
                  <Group justify="space-between">
                    <div>
                      <Text fw={500}>
                        {item.name} {!item.is_available && <Badge color="gray">unavailable</Badge>}
                      </Text>
                      <Text size="sm" c="dimmed">
                        {item.description} · stock {item.stock_count ?? "∞"}
                      </Text>
                    </div>
                    <Group gap="xs">
                      <Text>{money(item.price)}</Text>
                      <Button
                        size="xs"
                        disabled={!item.is_available}
                        onClick={() => setQty(item.id, (cart[item.id] ?? 0) + 1)}
                      >
                        Add
                      </Button>
                    </Group>
                  </Group>
                </Card>
              ))}
            </Stack>
          );
        })}
      </Grid.Col>

      <Grid.Col span={{ base: 12, md: 4 }}>
        <Card withBorder pos="sticky" top={72}>
          <Title order={4} mb="sm">
            Cart
          </Title>
          {lines.length === 0 && <Text c="dimmed">Empty</Text>}
          {lines.map((item) => (
            <Group key={item.id} justify="space-between" mb={4}>
              <Text size="sm">{item.name}</Text>
              <Group gap={4}>
                <ActionIcon variant="default" size="sm" onClick={() => setQty(item.id, cart[item.id] - 1)}>
                  −
                </ActionIcon>
                <Text size="sm">{cart[item.id]}</Text>
                <ActionIcon variant="default" size="sm" onClick={() => setQty(item.id, cart[item.id] + 1)}>
                  +
                </ActionIcon>
              </Group>
            </Group>
          ))}
          <Text fw={600} mt="xs">
            Subtotal {money(subtotal)}
          </Text>
          <Text size="xs" c="dimmed" mb="sm">
            Tax, delivery fee and discount are added by the order service.
          </Text>

          <Stack gap="xs">
            <Select
              label="Drop-off"
              value={chosenAddress}
              onChange={(value) => setAddressId(value)}
              data={[
                ...addresses.map((a) => ({ value: a.id, label: `${a.label}: ${a.line1}, ${a.city}` })),
                { value: CUSTOM, label: "Enter coordinates" },
              ]}
            />
            {chosenAddress === CUSTOM && (
              <>
                <TextInput
                  label="Address text"
                  value={dropoff.text}
                  onChange={(e) => setDropoff({ ...dropoff, text: e.currentTarget.value })}
                />
                <Group grow>
                  <TextInput
                    label="Lat"
                    placeholder={(restaurant.lat + 0.02).toFixed(4)}
                    value={dropoff.lat}
                    onChange={(e) => setDropoff({ ...dropoff, lat: e.currentTarget.value })}
                  />
                  <TextInput
                    label="Lng"
                    placeholder={(restaurant.lng + 0.01).toFixed(4)}
                    value={dropoff.lng}
                    onChange={(e) => setDropoff({ ...dropoff, lng: e.currentTarget.value })}
                  />
                </Group>
              </>
            )}
            <Select
              label="Payment"
              value={payment}
              onChange={(value) => setPayment(value ?? "card_simulated")}
              data={[
                { value: "card_simulated", label: "Card (approves)" },
                { value: "card_simulated_decline", label: "Card (declines)" },
              ]}
            />
            <TextInput label="Promo code" value={promo} onChange={(e) => setPromo(e.currentTarget.value)} />
            <Textarea
              label="Instructions"
              value={instructions}
              onChange={(e) => setInstructions(e.currentTarget.value)}
            />
            <Button disabled={lines.length === 0} loading={placing} onClick={place}>
              Place order
            </Button>
          </Stack>
        </Card>
        <AddAddress
          onAdded={(address) => {
            setData({ ...data, addresses: [...addresses, address] });
            setAddressId(address.id);
          }}
          near={restaurant}
        />
      </Grid.Col>
    </Grid>
  );
}

function AddAddress({ onAdded, near }: { onAdded: (a: Address) => void; near: Restaurant }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ label: "Home", line1: "", city: near.city, lat: near.lat + 0.02, lng: near.lng + 0.01 });
  if (!open)
    return (
      <Button variant="subtle" mt="xs" onClick={() => setOpen(true)}>
        Save a new address
      </Button>
    );
  const save = async () => {
    const address = await run(() => api<Address>("POST", "/v1/users/me/addresses", form), "Address saved");
    if (address) {
      onAdded(address);
      setOpen(false);
    }
  };
  return (
    <Card withBorder mt="sm">
      <Stack gap="xs">
        <TextInput label="Label" value={form.label} onChange={(e) => setForm({ ...form, label: e.currentTarget.value })} />
        <TextInput label="Line 1" value={form.line1} onChange={(e) => setForm({ ...form, line1: e.currentTarget.value })} />
        <TextInput label="City" value={form.city} onChange={(e) => setForm({ ...form, city: e.currentTarget.value })} />
        <Group grow>
          <NumberInput label="Lat" decimalScale={5} value={form.lat} onChange={(v) => setForm({ ...form, lat: Number(v) })} />
          <NumberInput label="Lng" decimalScale={5} value={form.lng} onChange={(v) => setForm({ ...form, lng: Number(v) })} />
        </Group>
        <Group>
          <Button onClick={save}>Save</Button>
          <Button variant="default" onClick={() => setOpen(false)}>
            Cancel
          </Button>
        </Group>
      </Stack>
    </Card>
  );
}
