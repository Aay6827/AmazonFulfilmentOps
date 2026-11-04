from datetime import date

# Business-day transit time per region and service level
TRANSIT_DAYS = {
    "south": {"standard": 2, "express": 1},
    "west": {"standard": 3, "express": 1},
    "north": {"standard": 4, "express": 2},
    "east": {"standard": 4, "express": 2},
    "remote": {"standard": 6, "express": 4},
}

# Extra business days the delivery window stays open after the earliest date
WINDOW_BUFFER_DAYS = {"standard": 1, "express": 0}

CUTOFF_HOUR = 14          # orders at or after 14:00 ship next business day
BACKORDER_DAYS = 5        # extra business days when stock is not on hand

HOLIDAYS = {
    date(2026, 8, 15),    # Independence Day
    date(2026, 10, 2),    # Gandhi Jayanti
    date(2027, 1, 26),    # Republic Day
}
