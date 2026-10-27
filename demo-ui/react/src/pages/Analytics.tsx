// Order stats and peak hours from analytics-service, for a restaurant or (the
// admin's summary) the whole platform. Window: the last N days.
import { Card, Group, Progress, SegmentedControl, SimpleGrid, Stack, Text, Title } from "@mantine/core";
import { useState } from "react";
import { get } from "../api";
import { useLoad } from "../hooks";
import type { HourCount, OrderStats } from "../types";
import { Empty, percent } from "../ui";

interface Response {
  orders: OrderStats;
  peak_hours: HourCount[];
  rider_utilization?: { overall: number | null };
  failed_events?: { dead_lettered: number; saga_system_errors: number; total: number };
}

export default function Analytics({ path }: { path: string }) {
  const [days, setDays] = useState("7");
  const { data } = useLoad(() => {
    const from = new Date(Date.now() - Number(days) * 86400000).toISOString();
    return get<Response>(`${path}?from=${encodeURIComponent(from)}`);
  }, [path, days]);

  return (
    <Stack>
      <SegmentedControl
        w={300}
        value={days}
        onChange={setDays}
        data={[
          { value: "1", label: "24 h" },
          { value: "7", label: "7 days" },
          { value: "30", label: "30 days" },
        ]}
      />
      {!data ? (
        <Empty>Loading…</Empty>
      ) : (
        <>
          <SimpleGrid cols={{ base: 2, md: 5 }}>
            <Stat label="Orders" value={data.orders.total_orders} />
            <Stat label="Delivered" value={data.orders.delivered} />
            <Stat label="Rejected" value={data.orders.rejected} />
            <Stat label="Cancelled" value={data.orders.cancelled} />
            <Stat
              label="Avg delivery"
              value={data.orders.avg_delivery_minutes == null ? "—" : `${data.orders.avg_delivery_minutes.toFixed(0)} min`}
            />
            <Stat label="Acceptance rate" value={percent(data.orders.restaurant_acceptance_rate)} />
            <Stat label="Cancellation rate" value={percent(data.orders.cancellation_rate)} />
            <Stat label="Delivery success" value={percent(data.orders.delivery_success_rate)} />
            {data.rider_utilization && <Stat label="Rider utilization" value={percent(data.rider_utilization.overall)} />}
            {data.failed_events && <Stat label="Failed events" value={data.failed_events.total} />}
          </SimpleGrid>
          <PeakHours hours={data.peak_hours} />
        </>
      )}
    </Stack>
  );
}

function Stat({ label, value }: { label: string; value: string | number }) {
  return (
    <Card withBorder padding="sm">
      <Text size="xs" c="dimmed">
        {label}
      </Text>
      <Text fw={700} size="lg">
        {value}
      </Text>
    </Card>
  );
}

// Orders per hour of day, as bars scaled to the busiest hour.
function PeakHours({ hours }: { hours: HourCount[] }) {
  const max = Math.max(1, ...hours.map((h) => h.orders));
  const sorted = [...hours].sort((a, b) => a.hour - b.hour);
  return (
    <Card withBorder>
      <Title order={5} mb="xs">
        Orders by hour
      </Title>
      {sorted.length === 0 && <Text c="dimmed">No orders in this window</Text>}
      {sorted.map((h) => (
        <Group key={h.hour} gap="xs" wrap="nowrap">
          <Text size="xs" w={40} ff="monospace">
            {String(h.hour).padStart(2, "0")}:00
          </Text>
          <Progress value={(h.orders / max) * 100} flex={1} />
          <Text size="xs" w={30}>
            {h.orders}
          </Text>
        </Group>
      ))}
    </Card>
  );
}
