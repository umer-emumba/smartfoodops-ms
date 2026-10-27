// Response shapes from the services' OpenAPI specs (only the fields the UI uses).

export interface Restaurant {
  id: string;
  admin_user_id: string;
  name: string;
  address: string;
  city: string;
  lat: number;
  lng: number;
  contact_phone: string;
  cuisine_type: string | null;
  status: string;
  is_accepting_orders: boolean;
  max_concurrent_orders: number;
  avg_prep_time_minutes: number;
}

export interface MenuCategory {
  id: string;
  name: string;
}

export interface MenuItem {
  id: string;
  category_id: string;
  name: string;
  description: string;
  price: number;
  is_available: boolean;
  stock_count: number | null;
}

export interface Promotion {
  id: string;
  code: string;
  discount_type: "percentage" | "fixed_amount";
  discount_value: number;
  valid_from: string;
  valid_until: string;
  max_uses: number | null;
  times_used: number;
  is_active: boolean;
}

export interface Address {
  id: string;
  label: string;
  line1: string;
  line2: string | null;
  city: string;
  lat: number;
  lng: number;
  is_default: boolean;
}

export interface Order {
  id: string;
  order_number: number;
  customer_id: string;
  restaurant_id: string;
  status: string;
  status_reason: string | null;
  delivery_address_text: string;
  special_instructions: string | null;
  subtotal: number;
  tax_amount: number;
  delivery_fee: number;
  discount_amount: number;
  promo_code: string | null;
  total_amount: number;
  payment_method: string;
  refunded_amount: number;
  created_at: string;
  items?: { id: string; item_name_snapshot: string; quantity: number; unit_price: number }[];
  status_history?: {
    from_status: string | null;
    to_status: string;
    actor_role: string | null;
    reason: string | null;
    created_at: string;
  }[];
}

export interface Payment {
  amount: number;
  status: string;
  payment_method: string;
  failure_reason: string | null;
  refunded_amount: number;
}

export interface Delivery {
  id: string;
  order_id: string;
  status: string;
  rider_id: string | null;
  pickup_lat: number | null;
  pickup_lng: number | null;
  dropoff_lat: number | null;
  dropoff_lng: number | null;
  distance_km: number | null;
  food_ready_at: string | null;
  pickup_eta: string | null;
  dropoff_eta: string | null;
  reassignment_count: number;
}

export interface Rider {
  id: string;
  user_id: string;
  status: string;
  verification_status: string;
  vehicle_type: string;
  license_plate: string | null;
  last_lat: number | null;
  last_lng: number | null;
  created_at: string;
}

export interface Offer {
  delivery_id: string;
  order_id: string;
  pickup_lat: number;
  pickup_lng: number;
  dropoff_lat: number;
  dropoff_lng: number;
  delivery_distance_km: number;
  distance_to_pickup_km: number;
  expires_at: string;
}

export interface Notification {
  id: string;
  related_order_id: string | null;
  type: string;
  channel: string;
  content: string;
  created_at: string;
  read_at: string | null;
}

export interface OrderStats {
  total_orders: number;
  confirmed: number;
  rejected: number;
  cancelled: number;
  delivered: number;
  cancellation_rate: number | null;
  restaurant_acceptance_rate: number | null;
  delivery_success_rate: number | null;
  avg_delivery_minutes: number | null;
}

export interface HourCount {
  hour: number;
  orders: number;
}
