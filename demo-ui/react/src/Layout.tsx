import { AppShell, Badge, Button, Container, Group, Text } from "@mantine/core";
import type { ReactNode } from "react";
import { NavLink, useNavigate } from "react-router-dom";
import { getSession, logout, type Role } from "./api";

const NAV: Record<Role, [string, string][]> = {
  customer: [
    ["Restaurants", "/customer"],
    ["My orders", "/customer/orders"],
  ],
  restaurant_admin: [["My restaurant", "/restaurant"]],
  rider: [["Deliveries", "/rider"]],
  system_admin: [["Admin", "/admin"]],
};

export default function Layout({ children }: { children: ReactNode }) {
  const navigate = useNavigate();
  const user = getSession()!.user;
  const links = [...NAV[user.role], ["Notifications", "/notifications"]];

  return (
    <AppShell header={{ height: 56 }} padding="md">
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <Text fw={700}>SmartFoodOps</Text>
            {links.map(([label, to]) => (
              <Button key={to} component={NavLink} to={to} end variant="subtle" size="compact-sm">
                {label}
              </Button>
            ))}
          </Group>
          <Group gap="xs">
            <Text size="sm">{user.full_name}</Text>
            <Badge variant="outline">{user.role.replace("_", " ")}</Badge>
            <Button
              variant="default"
              size="compact-sm"
              onClick={async () => {
                await logout();
                navigate("/login");
              }}
            >
              Log out
            </Button>
          </Group>
        </Group>
      </AppShell.Header>
      <AppShell.Main>
        <Container size="lg">{children}</Container>
      </AppShell.Main>
    </AppShell>
  );
}
