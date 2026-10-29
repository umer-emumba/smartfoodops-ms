// Log in (one click for the seeded demo accounts) or register a new account.
import { Button, Card, Center, Divider, PasswordInput, Select, Stack, Tabs, Text, TextInput } from "@mantine/core";
import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { HOME, login, register, type Role, type User } from "../api";
import { DEMO_ACCOUNTS, DEMO_PASSWORD } from "../config";
import { run } from "../hooks";

export default function Login() {
  const navigate = useNavigate();
  const [busy, setBusy] = useState(false);
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [fullName, setFullName] = useState("");
  const [phone, setPhone] = useState("");
  const [role, setRole] = useState<Role>("customer");

  const go = async (action: () => Promise<User>) => {
    setBusy(true);
    const user = await run(action);
    setBusy(false);
    if (user) navigate(HOME[user.role]);
  };

  return (
    <Center mih="100vh" bg="gray.0">
      <Card withBorder w={420} padding="lg">
        <Text fw={700} size="xl" mb="xs">
          SmartFoodOps demo
        </Text>
        <Text size="sm" c="dimmed" mb="md">
          Local testing only. Each browser tab keeps its own login.
        </Text>
        <Tabs defaultValue="demo">
          <Tabs.List grow mb="md">
            <Tabs.Tab value="demo">Demo accounts</Tabs.Tab>
            <Tabs.Tab value="login">Log in</Tabs.Tab>
            <Tabs.Tab value="register">Register</Tabs.Tab>
          </Tabs.List>

          <Tabs.Panel value="demo">
            <Stack gap="xs">
              {DEMO_ACCOUNTS.map((account) => (
                <Button
                  key={account.email}
                  variant="light"
                  disabled={busy}
                  onClick={() => go(() => login(account.email, DEMO_PASSWORD))}
                >
                  {account.label}
                </Button>
              ))}
              <Text size="xs" c="dimmed">
                Seeded by <code>scripts/seed.py</code>. Run it first if login fails.
              </Text>
            </Stack>
          </Tabs.Panel>

          <Tabs.Panel value="login">
            <Stack gap="xs">
              <TextInput label="Email" value={email} onChange={(e) => setEmail(e.currentTarget.value)} />
              <PasswordInput label="Password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
              <Button loading={busy} onClick={() => go(() => login(email, password))}>
                Log in
              </Button>
            </Stack>
          </Tabs.Panel>

          <Tabs.Panel value="register">
            <Stack gap="xs">
              <TextInput label="Full name" value={fullName} onChange={(e) => setFullName(e.currentTarget.value)} />
              <TextInput label="Email" value={email} onChange={(e) => setEmail(e.currentTarget.value)} />
              <TextInput label="Phone" placeholder="+923001234567" value={phone} onChange={(e) => setPhone(e.currentTarget.value)} />
              <PasswordInput label="Password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
              <Select
                label="Role"
                value={role}
                onChange={(value) => setRole((value ?? "customer") as Role)}
                data={[
                  { value: "customer", label: "Customer" },
                  { value: "restaurant_admin", label: "Restaurant admin" },
                  { value: "rider", label: "Rider" },
                ]}
              />
              <Button
                loading={busy}
                onClick={() =>
                  go(() => register({ email, password, full_name: fullName, phone_number: phone, role }))
                }
              >
                Register
              </Button>
              <Divider />
              <Text size="xs" c="dimmed">
                New restaurants and riders need an admin's approval before they get orders.
              </Text>
            </Stack>
          </Tabs.Panel>
        </Tabs>
      </Card>
    </Center>
  );
}
