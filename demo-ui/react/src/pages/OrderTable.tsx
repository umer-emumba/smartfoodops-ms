import { Anchor, Table } from "@mantine/core";
import type { ReactNode } from "react";
import { Link } from "react-router-dom";
import type { Order } from "../types";
import { dateTime, money, StatusBadge } from "../ui";

// Orders, newest first, each linking to its detail page. `actions` adds a
// last column (e.g. the restaurant's or admin's buttons).
export default function OrderTable({ orders, actions }: { orders: Order[]; actions?: (o: Order) => ReactNode }) {
  return (
    <Table striped highlightOnHover>
      <Table.Thead>
        <Table.Tr>
          <Table.Th>#</Table.Th>
          <Table.Th>Placed</Table.Th>
          <Table.Th>Status</Table.Th>
          <Table.Th>Total</Table.Th>
          <Table.Th>Refunded</Table.Th>
          {actions && <Table.Th />}
        </Table.Tr>
      </Table.Thead>
      <Table.Tbody>
        {orders.map((o) => (
          <Table.Tr key={o.id}>
            <Table.Td>
              <Anchor component={Link} to={`/orders/${o.id}`}>
                #{o.order_number}
              </Anchor>
            </Table.Td>
            <Table.Td>{dateTime(o.created_at)}</Table.Td>
            <Table.Td>
              <StatusBadge status={o.status} />
            </Table.Td>
            <Table.Td>{money(o.total_amount)}</Table.Td>
            <Table.Td>{o.refunded_amount ? money(o.refunded_amount) : ""}</Table.Td>
            {actions && <Table.Td>{actions(o)}</Table.Td>}
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}
