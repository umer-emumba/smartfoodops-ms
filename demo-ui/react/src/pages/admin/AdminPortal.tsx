// The system admin's portal: every order (refund / mark delivered / cancel
// on the order page), users (block), restaurants and riders (approve or
// suspend), and the platform summary.
import { Button, Card, Group, Select, Table, Tabs, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { api, type User } from "../../api";
import { run, usePaged } from "../../hooks";
import type { Order, Restaurant, Rider } from "../../types";
import { dateTime, Empty, LoadMore, StatusBadge } from "../../ui";
import Analytics from "../Analytics";
import OrderTable from "../OrderTable";

const query = (params: Record<string, string | null>) => {
  const set = Object.entries(params).filter(([, v]) => v);
  return set.length ? `?${new URLSearchParams(set as [string, string][])}` : "";
};

const ORDER_STATUSES = ["created", "awaiting_restaurant_acceptance", "confirmed", "assigned", "picked_up", "delivered", "rejected", "cancelled"];
const labelled = (values: string[]) => values.map((v) => ({ value: v, label: v.replaceAll("_", " ") }));

export default function AdminPortal() {
  return (
    <Tabs defaultValue="orders" keepMounted={false}>
      <Tabs.List mb="md">
        <Tabs.Tab value="orders">Orders</Tabs.Tab>
        <Tabs.Tab value="users">Users</Tabs.Tab>
        <Tabs.Tab value="restaurants">Restaurants</Tabs.Tab>
        <Tabs.Tab value="riders">Riders</Tabs.Tab>
        <Tabs.Tab value="analytics">Analytics</Tabs.Tab>
      </Tabs.List>
      <Tabs.Panel value="orders">
        <Orders />
      </Tabs.Panel>
      <Tabs.Panel value="users">
        <Users />
      </Tabs.Panel>
      <Tabs.Panel value="restaurants">
        <Restaurants />
      </Tabs.Panel>
      <Tabs.Panel value="riders">
        <Riders />
      </Tabs.Panel>
      <Tabs.Panel value="analytics">
        <Analytics path="/v1/analytics/summary" />
      </Tabs.Panel>
    </Tabs>
  );
}

function Orders() {
  const [status, setStatus] = useState<string | null>(null);
  const list = usePaged<Order>(`/v1/orders/admin${query({ status })}`);
  return (
    <Card withBorder>
      <Group mb="sm">
        <Select placeholder="Any status" clearable value={status} onChange={setStatus} data={labelled(ORDER_STATUSES)} />
        <Button variant="default" onClick={list.reload}>
          Refresh
        </Button>
      </Group>
      {list.items.length === 0 ? <Empty>{list.loading ? "Loading…" : "No orders"}</Empty> : <OrderTable orders={list.items} />}
      <LoadMore {...list} />
    </Card>
  );
}

function Users() {
  const [role, setRole] = useState<string | null>(null);
  const [blocked, setBlocked] = useState<string | null>(null);
  const [q, setQ] = useState("");
  const list = usePaged<User>(`/v1/users${query({ role, is_blocked: blocked, q })}`);
  const toggle = async (user: User) => {
    const action = user.is_blocked ? "unblock" : "block";
    if (await run(() => api("POST", `/v1/users/${user.id}/${action}`), `User ${action}ed`)) list.reload();
  };
  return (
    <Card withBorder>
      <Group mb="sm">
        <TextInput placeholder="Search name or email" value={q} onChange={(e) => setQ(e.currentTarget.value)} />
        <Select
          placeholder="Any role"
          clearable
          value={role}
          onChange={setRole}
          data={labelled(["customer", "restaurant_admin", "rider", "system_admin"])}
        />
        <Select
          placeholder="Blocked or not"
          clearable
          value={blocked}
          onChange={setBlocked}
          data={[
            { value: "true", label: "Blocked" },
            { value: "false", label: "Not blocked" },
          ]}
        />
      </Group>
      <Table striped>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th>Email</Table.Th>
            <Table.Th>Role</Table.Th>
            <Table.Th>Joined</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {list.items.map((u) => (
            <Table.Tr key={u.id}>
              <Table.Td>{u.full_name}</Table.Td>
              <Table.Td>{u.email}</Table.Td>
              <Table.Td>{u.role.replaceAll("_", " ")}</Table.Td>
              <Table.Td>{dateTime(u.created_at)}</Table.Td>
              <Table.Td>
                {u.role !== "system_admin" && (
                  <Button size="compact-xs" color={u.is_blocked ? "green" : "red"} variant="light" onClick={() => toggle(u)}>
                    {u.is_blocked ? "Unblock" : "Block"}
                  </Button>
                )}
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <LoadMore {...list} />
    </Card>
  );
}

// Approve, or suspend (which also rejects a pending one), with an optional reason.
function Verdict({ path, status, approved, onDone }: { path: string; status: string; approved: string; onDone: () => void }) {
  const send = async (action: "approve" | "suspend") => {
    const reason = window.prompt(`Reason to ${action} (optional)`);
    if (reason === null) return; // dialog cancelled
    const done = action === "approve" ? "Approved" : "Suspended";
    if (await run(() => api("POST", `${path}/${action}`, reason ? { reason } : undefined), done)) onDone();
  };
  return (
    <Group gap={4}>
      {status !== approved && (
        <Button size="compact-xs" onClick={() => send("approve")}>
          Approve
        </Button>
      )}
      {status !== "suspended" && (
        <Button size="compact-xs" color="red" variant="light" onClick={() => send("suspend")}>
          {status === approved ? "Suspend" : "Reject"}
        </Button>
      )}
    </Group>
  );
}

function Restaurants() {
  const [status, setStatus] = useState<string | null>(null);
  const list = usePaged<Restaurant>(`/v1/restaurants/admin${query({ status })}`);
  return (
    <Card withBorder>
      <Group mb="sm">
        <Title order={5}>Restaurants</Title>
        <Select placeholder="Any status" clearable value={status} onChange={setStatus} data={labelled(["pending_approval", "active", "suspended"])} />
      </Group>
      <Table striped>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Name</Table.Th>
            <Table.Th>City</Table.Th>
            <Table.Th>Status</Table.Th>
            <Table.Th>Accepting</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {list.items.map((r) => (
            <Table.Tr key={r.id}>
              <Table.Td>{r.name}</Table.Td>
              <Table.Td>{r.city}</Table.Td>
              <Table.Td>
                <StatusBadge status={r.status} />
              </Table.Td>
              <Table.Td>{r.is_accepting_orders ? "yes" : "no"}</Table.Td>
              <Table.Td>
                <Verdict path={`/v1/restaurants/${r.id}`} status={r.status} approved="active" onDone={list.reload} />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <LoadMore {...list} />
    </Card>
  );
}

function Riders() {
  const [status, setStatus] = useState<string | null>(null);
  const list = usePaged<Rider>(`/v1/riders/admin${query({ verification_status: status })}`);
  return (
    <Card withBorder>
      <Group mb="sm">
        <Title order={5}>Riders</Title>
        <Select placeholder="Any verification" clearable value={status} onChange={setStatus} data={labelled(["pending_approval", "approved", "suspended"])} />
      </Group>
      <Table striped>
        <Table.Thead>
          <Table.Tr>
            <Table.Th>Rider</Table.Th>
            <Table.Th>Vehicle</Table.Th>
            <Table.Th>Shift</Table.Th>
            <Table.Th>Verification</Table.Th>
            <Table.Th>Joined</Table.Th>
            <Table.Th />
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {list.items.map((r) => (
            <Table.Tr key={r.id}>
              <Table.Td ff="monospace" fz="xs">
                {r.id.slice(0, 8)}
              </Table.Td>
              <Table.Td>
                {r.vehicle_type}
                {r.license_plate && ` · ${r.license_plate}`}
              </Table.Td>
              <Table.Td>
                <StatusBadge status={r.status} />
              </Table.Td>
              <Table.Td>
                <StatusBadge status={r.verification_status} />
              </Table.Td>
              <Table.Td>{dateTime(r.created_at)}</Table.Td>
              <Table.Td>
                <Verdict path={`/v1/riders/${r.id}`} status={r.verification_status} approved="approved" onDone={list.reload} />
              </Table.Td>
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      <LoadMore {...list} />
    </Card>
  );
}
