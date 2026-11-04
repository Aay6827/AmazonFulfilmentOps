from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel

Region = Literal["south", "west", "north", "east", "remote"]
ServiceLevel = Literal["standard", "express"]


class PromiseRequest(BaseModel):
    region: Region
    service_level: ServiceLevel = "standard"
    all_in_stock: bool = True
    order_time: datetime | None = None


class PromiseResponse(BaseModel):
    region: Region
    service_level: ServiceLevel
    ship_date: date
    earliest_delivery: date
    latest_delivery: date
    transit_business_days: int
    backordered: bool
    missed_cutoff: bool
