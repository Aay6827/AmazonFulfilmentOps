from datetime import datetime
from enum import Enum

from pydantic import BaseModel, Field


class OrderStatus(str, Enum):
    CREATED = "CREATED"
    CANCELLED = "CANCELLED"


class OrderItem(BaseModel):
    sku: str = Field(min_length=1)
    qty: int = Field(gt=0, le=100)


class OrderCreate(BaseModel):
    customer_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    items: list[OrderItem] = Field(min_length=1)


class Order(BaseModel):
    id: str
    customer_id: str
    region: str
    items: list[OrderItem]
    status: OrderStatus
    created_at: datetime
