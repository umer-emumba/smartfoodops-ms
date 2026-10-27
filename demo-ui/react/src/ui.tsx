// Small display helpers shared by the portals.
import { Badge, Button, Group, Table, Text } from "@mantine/core";
import type { ReactNode } from "react";
import type { FeedState } from "./hooks";

export const money = (n: number | null | undefined) => (n == null ? "—" : Number(n).toFixed(2));

export const time = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleTimeString() : "—");

export const dateTime = (iso: string | null | undefined) => (iso ? new Date(iso).toLocaleString() : "—");

export const percent = (n: number | null | undefined) => (n == null ? "—" : `${(n * 100).toFixed(0)}%`);

const STATUS_COLORS: Record<string, string> = {
  delivered: "green",
  active: "green",
  approved: "green",
  captured: "green",
  available: "green",
  confirmed: "blue",
  assigned: "blue",
  picked_up: "cyan",
  authorized: "blue",
  busy: "orange",
  awaiting_restaurant_acceptance: "yellow",
  pending_approval: "yellow",
  created: "gray",
  offline: "gray",
  rejected: "red",
  cancelled: "red",
  suspended: "red",
  failed: "red",
  voided: "red",
  refunded: "grape",
  partially_refunded: "grape",
};

export function StatusBadge({ status }: { status: string | null | undefined }) {
  if (!status) return <Text c="dimmed">—</Text>;
  return (
    <Badge color={STATUS_COLORS[status] ?? "gray"} variant="light">
      {status.replaceAll("_", " ")}
    </Badge>
  );
}

export function FeedBadge({ state }: { state: FeedState }) {
  const color = { connecting: "yellow", open: "green", closed: "red" }[state];
  return (
    <Badge color={color} variant="dot">
      live feed {state}
    </Badge>
  );
}

// Label/value rows; rows with a null/undefined value are skipped.
export function KeyValues({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <Table withRowBorders={false} verticalSpacing={4}>
      <Table.Tbody>
        {rows
          .filter(([, value]) => value != null && value !== "")
          .map(([label, value]) => (
            <Table.Tr key={label}>
              <Table.Td w={160} c="dimmed">
                {label}
              </Table.Td>
              <Table.Td>{value}</Table.Td>
            </Table.Tr>
          ))}
      </Table.Tbody>
    </Table>
  );
}

export function LoadMore({ hasMore, loading, more }: { hasMore: boolean; loading: boolean; more: () => void }) {
  if (!hasMore) return null;
  return (
    <Group justify="center" mt="sm">
      <Button variant="subtle" loading={loading} onClick={more}>
        Load more
      </Button>
    </Group>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return (
    <Text c="dimmed" ta="center" py="lg">
      {children}
    </Text>
  );
}
