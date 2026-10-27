import { Button, Card, Group, NumberInput, Select, Switch, Table, TextInput, Title } from "@mantine/core";
import { useState } from "react";
import { api, get } from "../../api";
import { run, useLoad } from "../../hooks";
import type { Promotion } from "../../types";
import { dateTime, Empty, money } from "../../ui";

// datetime-local wants "YYYY-MM-DDTHH:mm" in local time.
const localInput = (d: Date) => new Date(d.getTime() - d.getTimezoneOffset() * 60000).toISOString().slice(0, 16);

export default function Promotions({ restaurantId }: { restaurantId: string }) {
  const base = `/v1/restaurants/${restaurantId}/promotions`;
  const { data, reload } = useLoad(() => get<Promotion[]>(base), [restaurantId]);
  const [form, setForm] = useState(() => ({
    code: "",
    discount_type: "percentage",
    discount_value: 10 as number | string,
    valid_from: localInput(new Date()),
    valid_until: localInput(new Date(Date.now() + 7 * 86400000)),
    max_uses: "" as number | string,
  }));

  const call = async (method: string, path: string, body?: unknown, success?: string) => {
    if (await run(() => api(method, path, body), success)) reload();
  };

  const create = () =>
    call(
      "POST",
      base,
      {
        code: form.code,
        discount_type: form.discount_type,
        discount_value: String(form.discount_value),
        valid_from: new Date(form.valid_from).toISOString(),
        valid_until: new Date(form.valid_until).toISOString(),
        max_uses: form.max_uses === "" ? null : Number(form.max_uses),
      },
      "Promotion created",
    );

  return (
    <Card withBorder>
      <Title order={4} mb="sm">
        Promotions
      </Title>
      <Group align="end" mb="md">
        <TextInput label="Code" value={form.code} onChange={(e) => setForm({ ...form, code: e.currentTarget.value.toUpperCase() })} />
        <Select
          label="Type"
          w={150}
          value={form.discount_type}
          onChange={(v) => setForm({ ...form, discount_type: v ?? "percentage" })}
          data={[
            { value: "percentage", label: "Percentage" },
            { value: "fixed_amount", label: "Fixed amount" },
          ]}
        />
        <NumberInput label="Value" w={100} value={form.discount_value} onChange={(v) => setForm({ ...form, discount_value: v })} />
        <TextInput type="datetime-local" label="From" value={form.valid_from} onChange={(e) => setForm({ ...form, valid_from: e.currentTarget.value })} />
        <TextInput type="datetime-local" label="Until" value={form.valid_until} onChange={(e) => setForm({ ...form, valid_until: e.currentTarget.value })} />
        <NumberInput label="Max uses" w={100} min={1} value={form.max_uses} onChange={(v) => setForm({ ...form, max_uses: v })} />
        <Button disabled={!form.code} onClick={create}>
          Create
        </Button>
      </Group>
      {data?.length === 0 && <Empty>No promotions</Empty>}
      {!!data?.length && (
        <Table striped>
          <Table.Thead>
            <Table.Tr>
              <Table.Th>Code</Table.Th>
              <Table.Th>Discount</Table.Th>
              <Table.Th>Valid</Table.Th>
              <Table.Th>Used</Table.Th>
              <Table.Th>Active</Table.Th>
              <Table.Th />
            </Table.Tr>
          </Table.Thead>
          <Table.Tbody>
            {data.map((p) => (
              <Table.Tr key={p.id}>
                <Table.Td>{p.code}</Table.Td>
                <Table.Td>{p.discount_type === "percentage" ? `${p.discount_value}%` : money(p.discount_value)}</Table.Td>
                <Table.Td>
                  {dateTime(p.valid_from)} → {dateTime(p.valid_until)}
                </Table.Td>
                <Table.Td>
                  {p.times_used}
                  {p.max_uses != null && ` / ${p.max_uses}`}
                </Table.Td>
                <Table.Td>
                  <Switch checked={p.is_active} onChange={(e) => call("PATCH", `${base}/${p.id}`, { is_active: e.currentTarget.checked }, "Saved")} />
                </Table.Td>
                <Table.Td>
                  <Button size="compact-xs" color="red" variant="subtle" onClick={() => call("DELETE", `${base}/${p.id}`, undefined, "Deleted")}>
                    Delete
                  </Button>
                </Table.Td>
              </Table.Tr>
            ))}
          </Table.Tbody>
        </Table>
      )}
    </Card>
  );
}
