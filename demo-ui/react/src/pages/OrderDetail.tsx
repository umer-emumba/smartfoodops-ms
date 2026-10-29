// One order, for whoever may see it: the customer and admins get the live
// feed, payment and delivery; the restaurant sees the order itself. Each
// role gets its own actions.
import { Anchor, Button, Card, Grid, Group, NumberInput, ScrollArea, Stack, Table, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { useParams } from "react-router-dom";
import { api, find, getSession } from "../api";
import { workflowUrl } from "../config";
import { run, useFeed, useLoad } from "../hooks";
import type { Delivery, Order, Payment } from "../types";
import { dateTime, Empty, FeedBadge, KeyValues, money, StatusBadge, time } from "../ui";

const FINISHED = ["delivered", "rejected", "cancelled"];
const CANCELLABLE = ["created", "awaiting_restaurant_acceptance", "confirmed", "assigned"];

interface LogLine {
  at: string;
  text: string;
}

export default function OrderDetail() {
  const { id } = useParams() as { id: string };
  const role = getSession()!.user.role;
  const { data, reload } = useLoad(async () => {
    const [order, payment, delivery] = await Promise.all([
      find<Order>(`/v1/orders/${id}`),
      find<Payment>(`/v1/payments/by-order/${id}`),
      find<Delivery>(`/v1/deliveries/by-order/${id}`),
    ]);
    return { order, payment, delivery };
  }, [id]);
  const [events, setEvents] = useState<LogLine[]>([]);
  const [rider, setRider] = useState<{ lat: number; lng: number } | null>(null);

  const log = (text: string) => setEvents((prev) => [{ at: new Date().toISOString(), text }, ...prev].slice(0, 100));
  const order = data?.order;
  // The stream ends by itself once the order is finished; don't reopen it then.
  const live = (role === "customer" || role === "system_admin") && order && !FINISHED.includes(order.status);
  const feed = useFeed(live ? `/v1/tracking/orders/${id}` : null, {
    snapshot: (s) => {
      log(`snapshot: ${s.status}`);
      reload();
    },
    status: (e) => {
      log(`status: ${e.status}${e.reason ? ` (${e.reason})` : ""}`);
      reload();
    },
    delivery: (e) => {
      const eta = e.pickup_eta ? `, pickup ETA ${time(e.pickup_eta)}, drop-off ETA ${time(e.dropoff_eta)}` : "";
      log(`delivery: ${e.status}${e.food_ready_at ? " — food ready" : ""}${eta}`);
      reload();
    },
    location: (e) => setRider({ lat: e.lat, lng: e.lng }),
  });

  if (!data) return <Empty>Loading…</Empty>;
  if (!order) return <Empty>Order not found, or not yours to see.</Empty>;
  const { payment, delivery } = data;

  const act = async (path: string, body?: unknown, success?: string) => {
    if (await run(() => api("POST", `/v1/orders/${id}/${path}`, body), success)) setTimeout(reload, 1500);
  };

  return (
    <Grid>
      <Grid.Col span={{ base: 12, md: 7 }}>
        <Card withBorder>
          <Group justify="space-between" mb="sm">
            <Title order={3}>Order #{order.order_number}</Title>
            <Group gap="xs">
              <StatusBadge status={order.status} />
              {live && <FeedBadge state={feed} />}
            </Group>
          </Group>
          <KeyValues
            rows={[
              ["Status reason", order.status_reason],
              ["Placed", dateTime(order.created_at)],
              ["Drop-off", order.delivery_address_text],
              ["Instructions", order.special_instructions],
              [
                "Total",
                `${money(order.subtotal)} + tax ${money(order.tax_amount)} + fee ${money(order.delivery_fee)}` +
                  (order.discount_amount ? ` − discount ${money(order.discount_amount)}` : "") +
                  ` = ${money(order.total_amount)}`,
              ],
              ["Promo", order.promo_code],
              ["Refunded", order.refunded_amount ? money(order.refunded_amount) : null],
              [
                "Payment",
                payment && (
                  <>
                    <StatusBadge status={payment.status} /> {payment.payment_method}
                    {payment.failure_reason && ` (${payment.failure_reason})`}
                  </>
                ),
              ],
              [
                "Delivery",
                delivery && (
                  <>
                    <StatusBadge status={delivery.status} />{" "}
                    {delivery.distance_km != null && `${delivery.distance_km.toFixed(1)} km, `}
                    reassigned {delivery.reassignment_count}×
                  </>
                ),
              ],
              ["Food ready", delivery?.food_ready_at ? time(delivery.food_ready_at) : null],
              ["Pickup ETA", delivery?.pickup_eta ? time(delivery.pickup_eta) : null],
              ["Drop-off ETA", delivery?.dropoff_eta ? time(delivery.dropoff_eta) : null],
              ["Rider at", rider && `${rider.lat.toFixed(4)}, ${rider.lng.toFixed(4)}`],
              [
                "Workflow",
                <Anchor href={workflowUrl(order.id)} target="_blank" rel="noopener">
                  Temporal UI
                </Anchor>,
              ],
            ]}
          />
          <Table mt="sm">
            <Table.Thead>
              <Table.Tr>
                <Table.Th>Item</Table.Th>
                <Table.Th>Qty</Table.Th>
                <Table.Th>Unit price</Table.Th>
              </Table.Tr>
            </Table.Thead>
            <Table.Tbody>
              {order.items?.map((item) => (
                <Table.Tr key={item.id}>
                  <Table.Td>{item.item_name_snapshot}</Table.Td>
                  <Table.Td>{item.quantity}</Table.Td>
                  <Table.Td>{money(item.unit_price)}</Table.Td>
                </Table.Tr>
              ))}
            </Table.Tbody>
          </Table>
        </Card>
      </Grid.Col>

      <Grid.Col span={{ base: 12, md: 5 }}>
        <Stack>
          <Actions order={order} role={role} act={act} />
          <Card withBorder>
            <Title order={5} mb="xs">
              Status history
            </Title>
            {order.status_history?.map((h) => (
              <Text key={h.created_at + h.to_status} size="sm" component="div">
                {time(h.created_at)} · <StatusBadge status={h.to_status} />{" "}
                {h.actor_role && <Text span c="dimmed">by {h.actor_role}</Text>}
                {h.reason && ` — ${h.reason}`}
              </Text>
            ))}
          </Card>
          {(live || events.length > 0) && (
            <Card withBorder>
              <Title order={5} mb="xs">
                Live events
              </Title>
              <ScrollArea h={200}>
                {events.length === 0 && <Text c="dimmed">None yet</Text>}
                {events.map((e, i) => (
                  <Text key={i} size="xs" ff="monospace">
                    {time(e.at)} {e.text}
                  </Text>
                ))}
              </ScrollArea>
            </Card>
          )}
        </Stack>
      </Grid.Col>
    </Grid>
  );
}

type Act = (path: string, body?: unknown, success?: string) => Promise<void>;

function Actions({ order, role, act }: { order: Order; role: string; act: Act }) {
  const [reason, setReason] = useState("");
  const [amount, setAmount] = useState<number | string>("");
  const buttons = [];

  if ((role === "customer" || role === "system_admin") && CANCELLABLE.includes(order.status)) {
    buttons.push(
      <Button key="cancel" color="red" variant="light" onClick={() => act("cancel", { reason: reason || null }, "Cancel requested")}>
        Cancel order
      </Button>,
    );
  }
  if (role === "restaurant_admin" && order.status === "awaiting_restaurant_acceptance") {
    buttons.push(
      <Button key="accept" onClick={() => act("restaurant-response", { decision: "accept" }, "Accepted")}>
        Accept
      </Button>,
      <Button
        key="reject"
        color="red"
        variant="light"
        onClick={() => act("restaurant-response", { decision: "reject", reason: reason || "Kitchen busy" }, "Rejected")}
      >
        Reject
      </Button>,
    );
  }
  if (role === "restaurant_admin" && ["confirmed", "assigned"].includes(order.status)) {
    buttons.push(
      <Button key="ready" onClick={() => act("food-ready", undefined, "Marked ready")}>
        Mark food ready
      </Button>,
    );
  }
  if (role === "system_admin" && order.status === "picked_up") {
    buttons.push(
      <Button key="delivered" variant="light" onClick={() => act("mark-delivered", undefined, "Marked delivered")}>
        Mark delivered
      </Button>,
    );
  }
  const refundable = role === "system_admin" && order.status === "delivered" && order.refunded_amount < order.total_amount;

  if (buttons.length === 0 && !refundable) return null;
  return (
    <Card withBorder>
      <Title order={5} mb="xs">
        Actions
      </Title>
      <Stack gap="xs">
        <TextInput label="Reason (optional)" value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Group>{buttons}</Group>
        {refundable && (
          <Group align="end">
            <NumberInput
              label="Refund amount (empty = all)"
              decimalScale={2}
              max={order.total_amount - order.refunded_amount}
              value={amount}
              onChange={setAmount}
            />
            <Button
              color="grape"
              onClick={() =>
                act("refund", { reason: reason || "Refund", ...(amount !== "" ? { amount: String(amount) } : {}) }, "Refund requested")
              }
            >
              Refund
            </Button>
          </Group>
        )}
      </Stack>
    </Card>
  );
}
