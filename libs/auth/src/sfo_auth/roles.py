from enum import StrEnum


class Role(StrEnum):
    CUSTOMER = "customer"
    RESTAURANT_ADMIN = "restaurant_admin"
    RIDER = "rider"
    SYSTEM_ADMIN = "system_admin"
