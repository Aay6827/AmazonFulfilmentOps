from typing import Literal

from pydantic import BaseModel, Field


class CheckoutItem(BaseModel):
    sku: str = Field(min_length=1)
    qty: int = Field(gt=0, le=100)


class CheckoutRequest(BaseModel):
    customer_id: str = Field(min_length=1)
    region: Literal["south", "west", "north", "east", "remote"]
    service_level: Literal["standard", "express"] = "standard"
    items: list[CheckoutItem] = Field(min_length=1)
