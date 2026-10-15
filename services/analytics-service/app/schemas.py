from datetime import datetime

from pydantic import BaseModel


class WindowResponse(BaseModel):
    start: datetime
    end: datetime


class OrderStats(BaseModel):
    total_orders: int
    confirmed: int
    rejected: int
    cancelled: int
    delivered: int
    cancellation_rate: float | None
    # confirmed ÷ (confirmed + rejected by or timed out at the restaurant)
    restaurant_acceptance_rate: float | None
    # delivered ÷ deliveries a rider accepted
    delivery_success_rate: float | None
    # placed → delivered
    avg_delivery_minutes: float | None
    saga_system_errors: int


class HourCount(BaseModel):
    hour: int
    orders: int


class RestaurantCount(BaseModel):
    restaurant_id: str
    orders: int


class RiderUtilizationEntry(BaseModel):
    rider_id: str
    on_shift_seconds: float
    on_delivery_seconds: float
    utilization: float | None


class RiderUtilization(BaseModel):
    overall: float | None
    riders: list[RiderUtilizationEntry]


class FailedEvents(BaseModel):
    dead_lettered: int
    saga_system_errors: int
    total: int


class SummaryResponse(BaseModel):
    window: WindowResponse
    orders: OrderStats
    orders_per_restaurant: list[RestaurantCount]
    peak_hours: list[HourCount]
    rider_utilization: RiderUtilization
    failed_events: FailedEvents


class RestaurantAnalyticsResponse(BaseModel):
    restaurant_id: str
    window: WindowResponse
    orders: OrderStats
    peak_hours: list[HourCount]


class PeakHoursResponse(BaseModel):
    window: WindowResponse
    timezone: str
    peak_hours: list[HourCount]
