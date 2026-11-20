"""Strict, side-effect-free decoder for RECOVERY inventory observations."""
from dataclasses import dataclass
from decimal import Decimal
import re

MAX_BYTES = 65536
MAX_ROWS = 128
UINT = re.compile(r"(?:0|[1-9][0-9]{0,19})", re.ASCII)
FIXED = re.compile(r"(?:0|[1-9][0-9]{0,14})\.[0-9]{16}", re.ASCII)
HEX = re.compile(r"[0-9a-f]{32}", re.ASCII)


def _uint(value: str, *, minimum: int = 0, maximum: int = (1 << 64) - 1) -> int:
    if not UINT.fullmatch(value):
        raise ValueError("invalid unsigned integer")
    result = int(value)
    if not minimum <= result <= maximum:
        raise ValueError("out of bounds unsigned integer")
    return result


def _decimal(value: str, *, positive: bool = False) -> Decimal:
    if not FIXED.fullmatch(value):
        raise ValueError("invalid fixed decimal")
    result = Decimal(value)
    if positive and result <= 0:
        raise ValueError("nonpositive decimal")
    return result


def _text(value: str) -> bool:
    return 0 < len(value.encode("utf-16-le")) // 2 <= 100 and all(
        ord(c) >= 32 and c not in ("\x7f", "\ufffd") for c in value
    )


@dataclass(frozen=True)
class InventoryScope:
    nonce: str
    executor_epoch: str
    verifier_epoch: str
    generation: int
    login: int
    server: str
    symbol: str
    magic: int
    verifier_chart: int

    def __post_init__(self):
        for value in (self.nonce, self.executor_epoch, self.verifier_epoch):
            if not isinstance(value, str) or not HEX.fullmatch(value):
                raise ValueError("invalid nonce or epoch")
        if self.executor_epoch == self.verifier_epoch:
            raise ValueError("roles share an epoch")
        for value, minimum in (
            (self.generation, 0),
            (self.login, 1),
            (self.magic, 1),
            (self.verifier_chart, 1),
        ):
            if type(value) is not int or not minimum <= value < 1 << 63:
                raise ValueError("invalid numeric scope")
        if not _text(self.server) or not _text(self.symbol):
            raise ValueError("invalid context field")

    def _wire(self) -> tuple[str, ...]:
        return (
            self.nonce, self.executor_epoch, self.verifier_epoch, str(self.generation),
            str(self.login), self.server, self.symbol, str(self.magic), str(self.verifier_chart),
        )

    def request_bytes(self) -> bytes:
        """Caller must publish these bytes create-only under `<nonce>.inv`."""
        return ("\t".join(("RECOVERY_INVENTORY_REQUEST_V1", *self._wire()[:-1], "END")) + "\n").encode()


@dataclass(frozen=True)
class Position:
    identifier: int
    ticket: int
    magic: int
    side: str
    volume: Decimal
    open_price: Decimal
    sl: Decimal
    tp: Decimal


@dataclass(frozen=True)
class Order:
    ticket: int
    magic: int
    position_identifier: int
    mt5_type: int
    mt5_state: int
    volume_initial: Decimal
    volume_current: Decimal
    open_price: Decimal
    sl: Decimal
    tp: Decimal
    stoplimit: Decimal


@dataclass(frozen=True)
class Snapshot:
    scope: InventoryScope
    sequence: int
    coverage: str
    reason: str
    positions: tuple[Position, ...]
    orders: tuple[Order, ...]

    @property
    def complete(self) -> bool:
        return self.coverage == "complete"


def decode_snapshot(raw: bytes, expected: InventoryScope) -> Snapshot:
    """Decode one valid and exactly bound record; invalid records raise ValueError."""
    if not isinstance(raw, bytes) or not 0 < len(raw) <= MAX_BYTES:
        raise ValueError("invalid record size")
    lines = raw.decode("utf-8").split("\n")
    if len(lines) < 3 or lines[-2:] != ["END", ""]:
        raise ValueError("missing terminal marker")
    head = lines[0].split("\t")
    if len(head) != 15 or head[0] != "RECOVERY_INVENTORY_V1" or tuple(head[1:10]) != expected._wire():
        raise ValueError("invalid scope or header")
    sequence = _uint(head[10], minimum=1, maximum=(1 << 63) - 1)
    coverage, reason = head[11:13]
    np, no = (_uint(x, maximum=MAX_ROWS) for x in head[13:15])
    body = lines[1:-2]
    if len(body) != np + no:
        raise ValueError("invalid record counts")
    if coverage == "unavailable":
        if reason not in {"read", "changed", "limit", "session"} or body:
            raise ValueError("invalid unavailable snapshot")
        return Snapshot(expected, sequence, coverage, reason, (), ())
    if coverage != "complete" or reason != "ok":
        raise ValueError("invalid complete snapshot")
    positions = []
    orders = []
    ids, ptickets, otickets = set(), set(), set()
    for line in body[:np]:
        parts = line.split("\t")
        if len(parts) != 9 or parts[0] != "P" or parts[4] not in {"BUY", "SELL"}:
            raise ValueError("invalid position row")
        identifier, ticket = (_uint(x, minimum=1) for x in parts[1:3])
        if identifier in ids or ticket in ptickets:
            raise ValueError("duplicate position identity")
        ids.add(identifier)
        ptickets.add(ticket)
        positions.append(Position(
            identifier, ticket, _uint(parts[3]), parts[4],
            _decimal(parts[5], positive=True), _decimal(parts[6], positive=True),
            _decimal(parts[7]), _decimal(parts[8]),
        ))
    for line in body[np:]:
        parts = line.split("\t")
        if len(parts) != 12 or parts[0] != "O":
            raise ValueError("invalid order row")
        ticket = _uint(parts[1], minimum=1)
        if ticket in otickets:
            raise ValueError("duplicate order ticket")
        otickets.add(ticket)
        initial, current = _decimal(parts[6], positive=True), _decimal(parts[7])
        if current > initial:
            raise ValueError("current volume exceeds initial")
        orders.append(Order(
            ticket, _uint(parts[2]), _uint(parts[3]), _uint(parts[4]), _uint(parts[5]),
            initial, current, *(_decimal(x) for x in parts[8:12]),
        ))
    return Snapshot(expected, sequence, coverage, reason, tuple(positions), tuple(orders))
