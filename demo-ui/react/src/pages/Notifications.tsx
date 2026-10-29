import { Anchor, Button, Card, Group, Switch, Table, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { run, usePaged } from "../hooks";
import type { Notification } from "../types";
import { dateTime, Empty, LoadMore } from "../ui";

export default function Notifications() {
  const [unreadOnly, setUnreadOnly] = useState(false);
  const list = usePaged<Notification>(`/v1/notifications${unreadOnly ? "?unread=true" : ""}`);

  const markRead = async (id: string) => {
    if (await run(() => api("POST", `/v1/notifications/${id}/read`))) list.reload();
  };

  return (
    <Card withBorder>
      <Group justify="space-between" mb="sm">
        <Title order={3}>Notifications</Title>
        <Group>
          <Switch label="Unread only" checked={unreadOnly} onChange={(e) => setUnreadOnly(e.currentTarget.checked)} />
          <Button variant="default" size="xs" onClick={list.reload}>
            Refresh
          </Button>
        </Group>
      </Group>
      {list.items.length === 0 ? (
        <Empty>{list.loading ? "Loading…" : "No notifications"}</Empty>
      ) : (
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>When</Table.Th>
              <Table.Th>Type</Table.Th>
              <Table.Th>Channel</Table.Th>
              <Table.Th>Message</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {list.items.map((n) => (
              <Table.Tr key={n.id} fw={n.read_at ? undefined : 600}>
                <Table.Td>{dateTime(n.created_at)}</Table.Td>
                <Table.Td>{n.type}</Table.Td>
                <Table.Td>{n.channel}</Table.Td>
                <Table.Td>
                  {n.content}
                  {n.related_order_id && (
                    <>
                      {" "}
                      <Anchor component={Link} to={`/orders/${n.related_order_id}`} size="sm">
                        order
                      </Anchor>
                    </>
                  )}
                </Table.Td>
                <Table.Td>
                  {!n.read_at && (
                    <Button size="compact-xs" variant="subtle" onClick={() => markRead(n.id)}>
                      Mark read
                    </Button>
                  )}
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
      <LoadMore {...list} />
    </Card>
  );
}
