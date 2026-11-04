from datetime import UTC, date, datetime
from uuid import uuid4

from . import config
from .models import (
    Reason,
    Return,
    ReturnCreate,
    ReturnEvent,
    ReturnStatus,
)


class ReturnWindowExpired(Exception):
    pass


class DuplicateReturn(Exception):
    pass


class InvalidTransition(Exception):
    pass


# action -> {current_status: next_status}
TRANSITIONS = {
    "approve": {ReturnStatus.REQUESTED: ReturnStatus.APPROVED},
    "reject": {
        ReturnStatus.REQUESTED: ReturnStatus.REJECTED,
        ReturnStatus.RECEIVED: ReturnStatus.REJECTED,
    },
    "receive": {ReturnStatus.APPROVED: ReturnStatus.RECEIVED},
    "refund": {ReturnStatus.RECEIVED: ReturnStatus.REFUNDED},
}


def _now() -> datetime:
    return datetime.now(UTC)


class ReturnStore:
    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self._returns: dict[str, Return] = {}

    def create(self, payload: ReturnCreate) -> Return:
        requested_on = payload.requested_on or date.today()
        if requested_on < payload.delivered_on:
            raise ValueError("requested_on is before delivered_on")
        if (requested_on - payload.delivered_on).days > config.RETURN_WINDOW_DAYS:
            raise ReturnWindowExpired("return window has expired")

        for existing in self._returns.values():
            if (
                existing.order_id == payload.order_id
                and existing.sku == payload.sku
                and existing.status != ReturnStatus.REJECTED
            ):
                raise DuplicateReturn("an active return already exists for this item")

        gross = payload.unit_price_paise * payload.qty
        fee = 0
        if payload.reason == Reason.CHANGED_MIND:
            fee = gross * config.RESTOCKING_FEE_PERCENT // 100

        ret = Return(
            id=f"RET-{uuid4().hex[:8].upper()}",
            order_id=payload.order_id,
            sku=payload.sku,
            qty=payload.qty,
            reason=payload.reason,
            status=ReturnStatus.REQUESTED,
            gross_paise=gross,
            restocking_fee_paise=fee,
            refund_paise=gross - fee,
            delivered_on=payload.delivered_on,
            requested_on=requested_on,
            history=[ReturnEvent(status=ReturnStatus.REQUESTED, at=_now())],
        )
        self._returns[ret.id] = ret
        return ret

    def get(self, return_id: str) -> Return | None:
        return self._returns.get(return_id)

    def list(self, order_id: str | None = None) -> list[Return]:
        items = list(self._returns.values())
        if order_id:
            items = [r for r in items if r.order_id == order_id]
        return items

    def transition(self, return_id: str, action: str) -> Return | None:
        ret = self._returns.get(return_id)
        if ret is None:
            return None
        nxt = TRANSITIONS[action].get(ret.status)
        if nxt is None:
            raise InvalidTransition(f"cannot {action} a return in {ret.status.value}")
        ret.status = nxt
        ret.history.append(ReturnEvent(status=nxt, at=_now()))
        return ret
