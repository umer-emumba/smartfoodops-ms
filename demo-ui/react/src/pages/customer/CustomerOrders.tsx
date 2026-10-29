import { Card, Title } from "@mantine/core";
import { usePaged } from "../../hooks";
import type { Order } from "../../types";
import { Empty, LoadMore } from "../../ui";
import OrderTable from "../OrderTable";

export default function CustomerOrders() {
  const list = usePaged<Order>("/v1/orders");
  return (
    <Card withBorder>
      <Title order={3} mb="sm">
        My orders
      </Title>
      {list.items.length === 0 ? <Empty>{list.loading ? "Loading…" : "No orders yet"}</Empty> : <OrderTable orders={list.items} />}
      <LoadMore {...list} />
    </Card>
  );
}
