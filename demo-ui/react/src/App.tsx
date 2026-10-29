import type { ReactNode } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import { getSession, HOME, type Role } from "./api";
import Layout from "./Layout";
import AdminPortal from "./pages/admin/AdminPortal";
import CustomerOrders from "./pages/customer/CustomerOrders";
import RestaurantList from "./pages/customer/RestaurantList";
import RestaurantMenu from "./pages/customer/RestaurantMenu";
import Login from "./pages/Login";
import Notifications from "./pages/Notifications";
import OrderDetail from "./pages/OrderDetail";
import RestaurantPortal from "./pages/restaurant/RestaurantPortal";
import RiderPortal from "./pages/rider/RiderPortal";

// Logged in, and (if `roles` is given) with one of those roles.
function Guard({ roles, children }: { roles?: Role[]; children: ReactNode }) {
  const session = getSession();
  if (!session) return <Navigate to="/login" replace />;
  if (roles && !roles.includes(session.user.role)) return <Navigate to={HOME[session.user.role]} replace />;
  return <Layout>{children}</Layout>;
}

export default function App() {
  const session = getSession();
  return (
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/customer" element={<Guard roles={["customer"]}><RestaurantList /></Guard>} />
      <Route path="/customer/restaurants/:id" element={<Guard roles={["customer"]}><RestaurantMenu /></Guard>} />
      <Route path="/customer/orders" element={<Guard roles={["customer"]}><CustomerOrders /></Guard>} />
      <Route path="/restaurant" element={<Guard roles={["restaurant_admin"]}><RestaurantPortal /></Guard>} />
      <Route path="/rider" element={<Guard roles={["rider"]}><RiderPortal /></Guard>} />
      <Route path="/admin" element={<Guard roles={["system_admin"]}><AdminPortal /></Guard>} />
      <Route path="/orders/:id" element={<Guard><OrderDetail /></Guard>} />
      <Route path="/notifications" element={<Guard><Notifications /></Guard>} />
      <Route path="*" element={<Navigate to={session ? HOME[session.user.role] : "/login"} replace />} />
    </Routes>
  );
}
