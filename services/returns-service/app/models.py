from datetime import date, datetime
from enum import Enum

from pydantic import BaseModel, Field


class Reason(str, Enum):
    DEFECTIVE = "DEFECTIVE"
    WRONG_ITEM = "WRONG_ITEM"
    DAMAGED_IN_TRANSIT = "DAMAGED_IN_TRANSIT"
    CHANGED_MIND = "CHANGED_MIND"


class ReturnStatus(str, Enum):
    REQUESTED = "REQUESTED"
    APPROVED = "APPROVED"
    RECEIVED = "RECEIVED"
    REFUNDED = "REFUNDED"
    REJECTED = "REJECTED"


class ReturnCreate(BaseModel):
    order_id: str = Field(min_length=1)
    sku: str = Field(min_length=1)
    qty: int = Field(gt=0, le=100)
    unit_price_paise: int = Field(gt=0)
    reason: Reason
    delivered_on: date
    requested_on: date | None = None


class ReturnEvent(BaseModel):
    status: ReturnStatus
    at: datetime


class Return(BaseModel):
    id: str
    order_id: str
    sku: str
    qty: int
    reason: Reason
    status: ReturnStatus
    gross_paise: int
    restocking_fee_paise: int
    refund_paise: int
    delivered_on: date
    requested_on: date
    history: list[ReturnEvent]
