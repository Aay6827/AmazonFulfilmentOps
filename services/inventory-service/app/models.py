from enum import Enum

from pydantic import BaseModel, Field


class ReservationStatus(str, Enum):
    RESERVED = "RESERVED"
    RELEASED = "RELEASED"


class StockSet(BaseModel):
    region: str = Field(min_length=1)
    on_hand: int = Field(ge=0)


class StockView(BaseModel):
    sku: str
    region: str
    on_hand: int
    reserved: int
    available: int


class ReserveItem(BaseModel):
    sku: str = Field(min_length=1)
    qty: int = Field(gt=0, le=100)


class ReserveRequest(BaseModel):
    order_id: str = Field(min_length=1)
    region: str = Field(min_length=1)
    items: list[ReserveItem] = Field(min_length=1)


class Reservation(BaseModel):
    order_id: str
    region: str
    items: list[ReserveItem]
    status: ReservationStatus
