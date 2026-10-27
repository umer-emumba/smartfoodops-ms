// Categories and items: add, rename, edit price/stock/availability, delete.
import { Button, Card, Group, NumberInput, Select, Stack, Switch, Table, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { api, get } from "../../api";
import { run, useLoad } from "../../hooks";
import type { MenuCategory, MenuItem } from "../../types";
import { Empty } from "../../ui";

export default function MenuManager({ restaurantId }: { restaurantId: string }) {
  const base = `/v1/restaurants/${restaurantId}/menu`;
  const { data, reload } = useLoad(async () => {
    const [categories, items] = await Promise.all([
      get<MenuCategory[]>(`${base}/categories`),
      get<MenuItem[]>(base),
    ]);
    return { categories, items };
  }, [restaurantId]);
  const [categoryName, setCategoryName] = useState("");

  if (!data) return <Empty>Loading…</Empty>;
  const call = async (method: string, path: string, body?: unknown, success?: string) => {
    if (await run(() => api(method, path, body), success)) reload();
  };

  return (
    <Stack>
      <Card withBorder>
        <Title order={4} mb="sm">
          Categories
        </Title>
        <Group mb="sm">
          <TextInput placeholder="New category" value={categoryName} onChange={(e) => setCategoryName(e.currentTarget.value)} />
          <Button
            disabled={!categoryName}
            onClick={async () => {
              await call("POST", `${base}/categories`, { name: categoryName }, "Category added");
              setCategoryName("");
            }}
          >
            Add
          </Button>
        </Group>
        <Group>
          {data.categories.map((c) => (
            <Group key={c.id} gap={4}>
              <TextInput
                size="xs"
                defaultValue={c.name}
                onBlur={(e) => e.currentTarget.value !== c.name && call("PATCH", `${base}/categories/${c.id}`, { name: e.currentTarget.value }, "Renamed")}
              />
              <Button size="compact-xs" color="red" variant="subtle" onClick={() => call("DELETE", `${base}/categories/${c.id}`, undefined, "Deleted")}>
                ✕
              </Button>
            </Group>
          ))}
        </Group>
      </Card>

      <Card withBorder>
        <Title order={4} mb="sm">
          Items
        </Title>
        <NewItem categories={data.categories} onAdd={(body) => call("POST", `${base}/items`, body, "Item added")} />
        <Table mt="md" striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Name</Table.Th>
              <Table.Th>Category</Table.Th>
              <Table.Th>Price</Table.Th>
              <Table.Th>Stock (empty = ∞)</Table.Th>
              <Table.Th>Available</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data.items.map((item) => (
              <ItemRow
                key={item.id}
                item={item}
                category={data.categories.find((c) => c.id === item.category_id)?.name ?? "—"}
                save={(patch) => call("PATCH", `${base}/items/${item.id}`, patch, "Saved")}
                remove={() => call("DELETE", `${base}/items/${item.id}`, undefined, "Deleted")}
              />
            ))}
          </Table.Tbody>
        </Table>
      </Card>
    </Stack>
  );
}

function ItemRow({
  item,
  category,
  save,
  remove,
}: {
  item: MenuItem;
  category: string;
  save: (patch: Record<string, unknown>) => void;
  remove: () => void;
}) {
  const [price, setPrice] = useState<number | string>(item.price);
  const [stock, setStock] = useState<number | string>(item.stock_count ?? "");
  const changed = Number(price) !== item.price || (stock === "" ? null : Number(stock)) !== item.stock_count;
  return (
    <Table.Tr>
      <Table.Td>{item.name}</Table.Td>
      <Table.Td>{category}</Table.Td>
      <Table.Td>
        <NumberInput size="xs" w={100} decimalScale={2} value={price} onChange={setPrice} />
      </Table.Td>
      <Table.Td>
        <NumberInput size="xs" w={100} min={0} value={stock} onChange={setStock} />
      </Table.Td>
      <Table.Td>
        <Switch checked={item.is_available} onChange={(e) => save({ is_available: e.currentTarget.checked })} />
      </Table.Td>
      <Table.Td>
        <Group gap={4} wrap="nowrap">
          <Button
            size="compact-xs"
            disabled={!changed}
            onClick={() => save({ price: String(price), stock_count: stock === "" ? null : Number(stock) })}
          >
            Save
          </Button>
          <Button size="compact-xs" color="red" variant="subtle" onClick={remove}>
            Delete
          </Button>
        </Group>
      </Table.Td>
    </Table.Tr>
  );
}

function NewItem({ categories, onAdd }: { categories: MenuCategory[]; onAdd: (body: unknown) => Promise<void> }) {
  const empty = { category_id: null as string | null, name: "", description: "", price: "" as number | string, stock: "" as number | string };
  const [form, setForm] = useState(empty);
  const categoryId = form.category_id ?? categories[0]?.id ?? null; // the first until one is picked
  return (
    <Group align="end">
      <Select
        label="Category"
        value={categoryId}
        onChange={(v) => setForm({ ...form, category_id: v })}
        data={categories.map((c) => ({ value: c.id, label: c.name }))}
      />
      <TextInput label="Name" value={form.name} onChange={(e) => setForm({ ...form, name: e.currentTarget.value })} />
      <TextInput label="Description" value={form.description} onChange={(e) => setForm({ ...form, description: e.currentTarget.value })} />
      <NumberInput label="Price" w={100} decimalScale={2} value={form.price} onChange={(v) => setForm({ ...form, price: v })} />
      <NumberInput label="Stock" w={100} min={0} value={form.stock} onChange={(v) => setForm({ ...form, stock: v })} />
      <Button
        disabled={!categoryId || !form.name || form.price === ""}
        onClick={async () => {
          await onAdd({
            category_id: categoryId,
            name: form.name,
            description: form.description,
            price: String(form.price),
            stock_count: form.stock === "" ? null : Number(form.stock),
          });
          setForm(empty);
        }}
      >
        Add item
      </Button>
    </Group>
  );
}
