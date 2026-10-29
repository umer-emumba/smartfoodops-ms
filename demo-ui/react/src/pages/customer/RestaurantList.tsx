import { Badge, Card, Group, SimpleGrid, Text, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { Link } from "react-router-dom";
import { usePaged } from "../../hooks";
import type { Restaurant } from "../../types";
import { Empty, LoadMore } from "../../ui";

export default function RestaurantList() {
  const [city, setCity] = useState("");
  const list = usePaged<Restaurant>(`/v1/restaurants${city ? `?city=${encodeURIComponent(city)}` : ""}`);

  return (
    <>
      <Group justify="space-between" mb="md">
        <Title order={3}>Restaurants</Title>
        <TextInput placeholder="Filter by city" value={city} onChange={(e) => setCity(e.currentTarget.value)} />
      </Group>
      {list.items.length === 0 && <Empty>{list.loading ? "Loading…" : "No restaurants"}</Empty>}
      <SimpleGrid cols={{ base: 1, sm: 2, md: 3 }}>
        {list.items.map((r) => (
          <Card key={r.id} withBorder component={Link} to={`/customer/restaurants/${r.id}`}>
            <Group justify="space-between">
              <Text fw={600}>{r.name}</Text>
              <Badge color={r.is_accepting_orders ? "green" : "gray"} variant="light">
                {r.is_accepting_orders ? "open" : "closed"}
              </Badge>
            </Group>
            <Text size="sm" c="dimmed">
              {[r.cuisine_type, r.city].filter(Boolean).join(" · ")}
            </Text>
            <Text size="sm">{r.address}</Text>
            <Text size="xs" c="dimmed">
              ~{r.avg_prep_time_minutes} min prep
            </Text>
          </Card>
        ))}
      </SimpleGrid>
      <LoadMore {...list} />
    </>
  );
}
