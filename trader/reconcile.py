from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RemoteSnapshot:
    positions: dict[str, dict]
    orders: dict[str, dict]

    def unknown_position_ids(self, known_deal_ids: set[str], trigger_ids: set[str]) -> set[str]:
        linked = {
            deal_id for deal_id, position in self.positions.items()
            if str(position.get("workingOrderId", "")) in trigger_ids
        }
        return set(self.positions) - known_deal_ids - linked

    def find_trigger_fill(self, direction: str, trigger_id: str) -> dict | None:
        return next((position for position in self.positions.values()
                     if position.get("direction") == direction
                     and str(position.get("workingOrderId", "")) == trigger_id), None)
