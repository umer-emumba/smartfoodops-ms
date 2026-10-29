// The rider's portal: profile (or sign-up), shift and position, the open
// offer, and the delivery they hold. Offers arrive on the rider's live feed
// (/v1/tracking/riders/me); each event — and each (re)connect's snapshot —
// is a nudge to re-read the offer or held delivery over REST. A slow safety
// poll covers the feed being down.
import { Alert, Button, Card, Group, NumberInput, Select, SimpleGrid, Stack, Text, TextInput, Title } from "@mantine/core";
import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, find, get, type Page } from "../../api";
import { run, showError, useFeed, useLoad, useTick } from "../../hooks";
import type { Delivery, Offer, Restaurant, Rider } from "../../types";
import { Empty, FeedBadge, KeyValues, StatusBadge, time } from "../../ui";

const SAFETY_POLL_MS = 30000;
const ENDED = ["delivered", "cancelled"];

// A point `km` north of `from` (1° of latitude ≈ 111 km).
const north = (from: { lat: number; lng: number }, km: number) => ({ lat: from.lat + km / 111, lng: from.lng });

export default function RiderPortal() {
  const { data: me, setData: setMe, reload: reloadMe } = useLoad(async () => {
    try {
      return await get<Rider>("/v1/riders/me");
    } catch (error) {
      if (error instanceof ApiError && error.status === 404) return null; // no rider profile yet
      throw error;
    }
  }, []);

  if (me === undefined) return <Empty>Loading…</Empty>;
  if (me === null) return <SignUp onDone={reloadMe} />;
  return <Shift me={me} setMe={setMe} />;
}

function SignUp({ onDone }: { onDone: () => void }) {
  const [vehicle, setVehicle] = useState("bike");
  const [plate, setPlate] = useState("");
  const submit = async () => {
    const body = { vehicle_type: vehicle, license_plate: plate || null };
    if (await run(() => api("POST", "/v1/riders", body), "Rider profile created")) onDone();
  };
  return (
    <Card withBorder maw={420}>
      <Title order={4} mb="sm">
        Create your rider profile
      </Title>
      <Stack gap="xs">
        <Select label="Vehicle" value={vehicle} onChange={(v) => setVehicle(v ?? "bike")} data={["bike", "scooter", "car"]} />
        <TextInput label="License plate" value={plate} onChange={(e) => setPlate(e.currentTarget.value)} />
        <Button onClick={submit}>Create</Button>
      </Stack>
    </Card>
  );
}

function Shift({ me, setMe }: { me: Rider; setMe: (r: Rider) => void }) {
  const { data: restaurants } = useLoad(() => get<Page<Restaurant>>("/v1/restaurants?limit=100"), []);
  const [restaurantId, setRestaurantId] = useState<string | null>(null);
  const [km, setKm] = useState<number | string>(0.3);
  const [manual, setManual] = useState({ lat: "", lng: "" });
  const [offer, setOffer] = useState<Offer | null>(null);
  const [delivery, setDelivery] = useState<Delivery | null>(null);
  const heldOrderId = useRef<string | null>(null);
  useTick(1000); // the offer's countdown

  const restaurant = restaurants?.items.find((r) => r.id === restaurantId) ?? restaurants?.items[0];

  // The offer or held delivery, re-read over REST (the source of truth).
  const refresh = useCallback(async () => {
    try {
      if (heldOrderId.current) {
        const current = await find<Delivery>(`/v1/deliveries/by-order/${heldOrderId.current}`);
        if (!current || current.rider_id !== me.id || ENDED.includes(current.status)) {
          heldOrderId.current = null;
          setDelivery(null);
          setMe(await get<Rider>("/v1/riders/me"));
        } else {
          setDelivery(current);
        }
      }
      if (!heldOrderId.current) setOffer(await find<Offer>("/v1/riders/me/offer"));
    } catch (error) {
      showError(error);
    }
  }, [me.id, setMe]);

  useEffect(() => {
    const id = setInterval(refresh, SAFETY_POLL_MS);
    return () => clearInterval(id);
  }, [refresh]);

  const feed = useFeed("/v1/tracking/riders/me", {
    snapshot: (s) => {
      // The delivery this rider has: an open offer, or one they accepted.
      if (s.delivery?.order_id && s.delivery.status !== "offered") heldOrderId.current = s.delivery.order_id;
      refresh();
    },
    offer: refresh,
    released: refresh,
    delivery: refresh,
    food_ready: refresh,
  });

  const move = async (point: { lat: number; lng: number }, label: string) => {
    const rider = await run(() => api<Rider>("PATCH", "/v1/riders/me/location", point), `Moved ${label}`);
    if (rider) setMe(rider);
  };

  const setStatus = async (status: "available" | "offline") => {
    const rider = await run(() => api<Rider>("PATCH", "/v1/riders/me/status", { status }));
    if (!rider) return;
    setMe(rider);
    if (status === "available" && restaurant) await move(north(restaurant, Number(km)), `${km} km from ${restaurant.name}`);
  };

  const act = async (action: string, body?: unknown) => {
    const id = offer?.delivery_id ?? delivery?.id;
    if (!id) return;
    if (!(await run(() => api("POST", `/v1/riders/me/deliveries/${id}/${action}`, body), `Sent: ${action}`))) return;
    if (action === "accept" && offer) heldOrderId.current = offer.order_id;
    if (action === "accept" || action === "decline") setOffer(null);
    if (action === "drop-out") {
      heldOrderId.current = null;
      setDelivery(null);
    }
    setTimeout(refresh, 500);
  };

  const secondsLeft = offer ? Math.max(0, Math.round((new Date(offer.expires_at).getTime() - Date.now()) / 1000)) : 0;

  return (
    <SimpleGrid cols={{ base: 1, md: 2 }}>
      <Stack>
        <Card withBorder>
          <Group justify="space-between" mb="sm">
            <Title order={4}>Me</Title>
            <FeedBadge state={feed} />
          </Group>
          {me.verification_status !== "approved" && (
            <Alert color="yellow" mb="sm">
              Verification is {me.verification_status.replaceAll("_", " ")}: you won't get offers until an admin approves you.
            </Alert>
          )}
          <KeyValues
            rows={[
              ["Status", <StatusBadge status={me.status} />],
              ["Verification", <StatusBadge status={me.verification_status} />],
              ["Vehicle", `${me.vehicle_type}${me.license_plate ? ` · ${me.license_plate}` : ""}`],
              ["Position", me.last_lat != null ? `${me.last_lat.toFixed(4)}, ${me.last_lng?.toFixed(4)}` : "unknown"],
            ]}
          />
        </Card>

        <Card withBorder>
          <Title order={4} mb="sm">
            Shift
          </Title>
          <Stack gap="xs">
            <Group grow align="end">
              <Select
                label="Start near"
                value={restaurant?.id ?? null}
                onChange={setRestaurantId}
                data={restaurants?.items.map((r) => ({ value: r.id, label: r.name })) ?? []}
              />
              <NumberInput label="km away" min={0} step={0.1} decimalScale={2} value={km} onChange={setKm} />
            </Group>
            <Group>
              <Button onClick={() => setStatus("available")}>Go on shift</Button>
              <Button variant="default" onClick={() => setStatus("offline")}>
                Go offline
              </Button>
              {restaurant && (
                <Button variant="subtle" onClick={() => move(north(restaurant, Number(km)), `${km} km from ${restaurant.name}`)}>
                  Move there
                </Button>
              )}
            </Group>
            <Group align="end">
              <TextInput label="Lat" w={120} value={manual.lat} onChange={(e) => setManual({ ...manual, lat: e.currentTarget.value })} />
              <TextInput label="Lng" w={120} value={manual.lng} onChange={(e) => setManual({ ...manual, lng: e.currentTarget.value })} />
              <Button
                variant="default"
                disabled={!manual.lat || !manual.lng}
                onClick={() => move({ lat: Number(manual.lat), lng: Number(manual.lng) }, "to the coordinates")}
              >
                Set position
              </Button>
            </Group>
          </Stack>
        </Card>
      </Stack>

      <Stack>
        <Card withBorder>
          <Group justify="space-between" mb="sm">
            <Title order={4}>Offer</Title>
            <Button size="xs" variant="default" onClick={refresh}>
              Check now
            </Button>
          </Group>
          {!offer ? (
            <Empty>No open offer</Empty>
          ) : (
            <>
              <Text>
                {offer.distance_to_pickup_km} km to pickup · {offer.delivery_distance_km.toFixed(1)} km delivery ·{" "}
                <b>{secondsLeft}s left</b>
              </Text>
              <Group mt="sm">
                <Button onClick={() => act("accept")}>Accept</Button>
                <Button color="red" variant="light" onClick={() => act("decline")}>
                  Decline
                </Button>
              </Group>
            </>
          )}
        </Card>

        <Card withBorder>
          <Title order={4} mb="sm">
            Current delivery
          </Title>
          {!delivery ? (
            <Empty>None</Empty>
          ) : (
            <>
              <KeyValues
                rows={[
                  ["Status", <StatusBadge status={delivery.status} />],
                  ["Food", delivery.food_ready_at ? `ready at ${time(delivery.food_ready_at)}` : "not ready yet"],
                  ["Pickup ETA", time(delivery.pickup_eta)],
                  ["Drop-off ETA", time(delivery.dropoff_eta)],
                  ["Distance", delivery.distance_km != null ? `${delivery.distance_km.toFixed(1)} km` : null],
                ]}
              />
              <Group mt="sm" gap="xs">
                {delivery.pickup_lat != null && (
                  <Button
                    variant="default"
                    onClick={() => move({ lat: delivery.pickup_lat!, lng: delivery.pickup_lng! }, "to the restaurant")}
                  >
                    Ride to restaurant
                  </Button>
                )}
                <Button onClick={() => act("picked-up")}>Picked up</Button>
                {delivery.dropoff_lat != null && (
                  <Button
                    variant="default"
                    onClick={() => move({ lat: delivery.dropoff_lat!, lng: delivery.dropoff_lng! }, "to the customer")}
                  >
                    Ride to customer
                  </Button>
                )}
                <Button color="green" onClick={() => act("delivered")}>
                  Delivered
                </Button>
                <Button color="red" variant="light" onClick={() => act("drop-out", { reason: "Can't complete" })}>
                  Drop out
                </Button>
              </Group>
            </>
          )}
        </Card>
      </Stack>
    </SimpleGrid>
  );
}
