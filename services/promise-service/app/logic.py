from datetime import date, datetime, timedelta

from . import config


def is_business_day(d: date) -> bool:
    return d.weekday() < 5 and d not in config.HOLIDAYS


def first_business_day_on_or_after(d: date) -> date:
    while not is_business_day(d):
        d += timedelta(days=1)
    return d


def next_business_day(d: date) -> date:
    return first_business_day_on_or_after(d + timedelta(days=1))


def add_business_days(d: date, n: int) -> date:
    for _ in range(n):
        d = next_business_day(d)
    return d


def compute_promise(
    region: str,
    service_level: str,
    all_in_stock: bool,
    order_time: datetime,
) -> dict:
    order_date = order_time.date()
    missed_cutoff = order_time.hour >= config.CUTOFF_HOUR

    if missed_cutoff:
        ship = next_business_day(order_date)
    else:
        ship = first_business_day_on_or_after(order_date)

    if not all_in_stock:
        ship = add_business_days(ship, config.BACKORDER_DAYS)

    transit = config.TRANSIT_DAYS[region][service_level]
    earliest = add_business_days(ship, transit)
    latest = add_business_days(earliest, config.WINDOW_BUFFER_DAYS[service_level])

    return {
        "region": region,
        "service_level": service_level,
        "ship_date": ship,
        "earliest_delivery": earliest,
        "latest_delivery": latest,
        "transit_business_days": transit,
        "backordered": not all_in_stock,
        "missed_cutoff": missed_cutoff,
    }
