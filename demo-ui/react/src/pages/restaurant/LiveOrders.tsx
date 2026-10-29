// Orders awaiting a decision (pushed on the restaurant's live feed) and
// accepted orders still being prepared (read over REST: the feed only
// carries new orders and cancellations).
import { Anchor, Button, Card, Group, SimpleGrid, Stack, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, get, type Page } from "../../api";
import { run, showError, useFeed } from "../../hooks";
import type { Order, Restaurant } from "../../types";
import { Empty, FeedBadge, money } from "../../ui";

interface Waiting {
  order_id: string;
  order_number: number;
  total_amount: number | string;
}

export default function LiveOrders({ restaurant }: { restaurant: Restaurant }) {
  const [pending, setPending] = useState<Waiting[]>([]);
  const [preparing, setPreparing] = useState<Order[]>([]);
  const [reason, setReason] = useState("Kitchen busy");

  const loadPreparing = useCallback(async () => {
    try {
      const pages = await Promise.all(
        ["confirmed", "assigned"].map((s) => get<Page<Order>>(`/v1/orders/restaurant/${restaurant.id}?status=${s}`)),
      );
      setPreparing(pages.flatMap((p) => p.items));
    } catch (error) {
      showError(error);
    }
  }, [restaurant.id]);
  useEffect(() => void loadPreparing(), [loadPreparing]);

  const drop = (orderId: string) => setPending((prev) => prev.filter((o) => o.order_id !== orderId));
  const feed = useFeed(`/v1/tracking/restaurants/${restaurant.id}`, {
    snapshot: (s) => setPending(s.pending),
    new_order: (e) => setPending((prev) => [...prev.filter((o) => o.order_id !== e.order_id), e]),
    order_cancelled: (e) => {
      drop(e.order_id);
      setPreparing((prev) => prev.filter((o) => o.id !== e.order_id));
    },
  });

  const respond = async (orderId: string, decision: "accept" | "reject") => {
    const body = decision === "reject" ? { decision, reason } : { decision };
    if (await run(() => api("POST", `/v1/orders/${orderId}/restaurant-response`, body), `Order ${decision}ed`)) {
      drop(orderId);
      if (decision === "accept") setTimeout(loadPreparing, 1000);
    }
  };

  const markReady = async (orderId: string) => {
    if (await run(() => api("POST", `/v1/orders/${orderId}/food-ready`), "Food marked ready")) {
      setPreparing((prev) => prev.filter((o) => o.id !== orderId));
    }
  };

  return (
    <SimpleGrid cols={{ base: 1, md: 2 }}>
      <Card withBorder>
        <Group justify="space-between" mb="sm">
          <Title order={4}>Waiting for you</Title>
          <FeedBadge state={feed} />
        </Group>
        <TextInput label="Reject reason" size="xs" mb="sm" value={reason} onChange={(e) => setReason(e.currentTarget.value)} />
        <Stack gap="xs">
          {pending.length === 0 && <Empty>No orders waiting</Empty>}
          {pending.map((o) => (
            <Card key={o.order_id} withBorder padding="xs">
              <Group justify="space-between">
                <Text>
                  <Anchor component={Link} to={`/orders/${o.order_id}`}>
                    #{o.order_number}
                  </Anchor>{" "}
                  · {money(Number(o.total_amount))}
                </Text>
                <Group gap="xs">
                  <Button size="xs" onClick={() => respond(o.order_id, "accept")}>
                    Accept
                  </Button>
                  <Button size="xs" color="red" variant="light" onClick={() => respond(o.order_id, "reject")}>
                    Reject
                  </Button>
                </Group>
              </Group>
            </Card>
          ))}
          <Text size="xs" c="dimmed">
            Unanswered orders time out after 5 minutes.
          </Text>
        </Stack>
      </Card>

      <Card withBorder>
        <Group justify="space-between" mb="sm">
          <Title order={4}>Preparing</Title>
          <Button size="xs" variant="default" onClick={loadPreparing}>
            Refresh
          </Button>
        </Group>
        <Stack gap="xs">
          {preparing.length === 0 && <Empty>Nothing being prepared</Empty>}
          {preparing.map((o) => (
            <Card key={o.id} withBorder padding="xs">
              <Group justify="space-between">
                <Text>
                  <Anchor component={Link} to={`/orders/${o.id}`}>
                    #{o.order_number}
                  </Anchor>{" "}
                  · {o.items?.map((i) => `${i.quantity}× ${i.item_name_snapshot}`).join(", ")}
                </Text>
                <Button size="xs" onClick={() => markReady(o.id)}>
                  Mark ready
                </Button>
              </Group>
            </Card>
          ))}
          <Text size="xs" c="dimmed">
            Orders already marked ready still show here after a refresh; marking twice is harmless.
          </Text>
        </Stack>
      </Card>
    </SimpleGrid>
  );
}
