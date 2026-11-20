"""Strict decoder for exact, position-linked historical observations."""
from dataclasses import dataclass
from decimal import Decimal

from record import InventoryScope, MAX_BYTES, MAX_ROWS, _decimal, _uint


@dataclass(frozen=True)
class HistoryScope:
    inventory_scope: InventoryScope
    position_identifier: int
    anchor_order: int

    def __post_init__(self):
        if any(type(x) is not int or not 0 < x < 1 << 64
               for x in (self.position_identifier, self.anchor_order)):
            raise ValueError("invalid position/order identity")

    def request_bytes(self) -> bytes:
        return ("\t".join((
            "RECOVERY_HISTORY_REQUEST_V1", *self.inventory_scope._wire()[:-1],
            str(self.position_identifier), str(self.anchor_order), "END",
        )) + "\n").encode()


@dataclass(frozen=True)
class HistoricalOrder:
    ticket: int
    magic: int
    type: int
    state: int
    time_done_msc: int
    initial_volume: Decimal
    remaining_volume: Decimal
    price: Decimal
    sl: Decimal
    tp: Decimal


@dataclass(frozen=True)
class Deal:
    ticket: int
    order_ticket: int
    magic: int
    side: str
    entry: int
    reason: int
    time_msc: int
    volume: Decimal
    price: Decimal
    sl: Decimal
    tp: Decimal


@dataclass(frozen=True)
class History:
    scope: HistoryScope
    sequence: int
    observed: bool
    reason: str
    orders: tuple[HistoricalOrder, ...]
    deals: tuple[Deal, ...]


def decode_history(raw: bytes, expected: HistoryScope) -> History:
    """An observed list is evidence only, not proof of complete position closure."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("invalid record size")
    lines = raw.decode("utf-8").split("\n")
    if len(lines) < 3 or lines[-2:] != ["END", ""]:
        raise ValueError("missing terminal marker")
    head = lines[0].split("\t")
    if (len(head) != 17 or head[0] != "RECOVERY_HISTORY_V1"
            or tuple(head[1:10]) != expected.inventory_scope._wire()
            or head[11:13] != [str(expected.position_identifier), str(expected.anchor_order)]):
        raise ValueError("invalid scope or header")
    sequence = _uint(head[10], minimum=1, maximum=(1 << 63) - 1)
    coverage, reason = head[13:15]
    no, nd = (_uint(x, maximum=MAX_ROWS) for x in head[15:17])
    rows = lines[1:-2]
    if len(rows) != no + nd:
        raise ValueError("invalid row counts")
    if coverage == "unavailable":
        if reason not in {"read", "changed", "limit", "session", "anchor_missing"} or rows:
            raise ValueError("invalid unavailable history")
        return History(expected, sequence, False, reason, (), ())
    if coverage != "observed" or reason != "ok":
        raise ValueError("invalid observed history")
    orders, deals = [], []
    order_ids, deal_ids = set(), set()
    anchored = False
    for row in rows[:no]:
        p = row.split("\t")
        if len(p) != 11 or p[0] != "H":
            raise ValueError("invalid order row")
        ticket = _uint(p[1], minimum=1)
        initial, remaining = _decimal(p[6], positive=True), _decimal(p[7])
        if ticket in order_ids or remaining > initial:
            raise ValueError("duplicate ticket or inconsistent volume")
        order_ids.add(ticket)
        anchored |= ticket == expected.anchor_order
        orders.append(HistoricalOrder(
            ticket, *(_uint(v) for v in p[2:6]), initial, remaining,
            *(_decimal(v) for v in p[8:11]),
        ))
    for row in rows[no:]:
        p = row.split("\t")
        if len(p) != 12 or p[0] != "D" or p[4] not in {"BUY", "SELL"}:
            raise ValueError("invalid deal row")
        ticket, order = (_uint(v, minimum=1) for v in p[1:3])
        if ticket in deal_ids:
            raise ValueError("duplicate deal ticket")
        deal_ids.add(ticket)
        anchored |= order == expected.anchor_order
        deals.append(Deal(
            ticket, order, _uint(p[3]), p[4], _uint(p[5]), _uint(p[6]), _uint(p[7], minimum=1),
            _decimal(p[8], positive=True), _decimal(p[9], positive=True),
            _decimal(p[10]), _decimal(p[11]),
        ))
    if not anchored:
        raise ValueError("history lacks exact anchor")
    return History(expected, sequence, True, reason, tuple(orders), tuple(deals))
