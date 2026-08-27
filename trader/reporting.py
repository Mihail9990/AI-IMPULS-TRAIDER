from __future__ import annotations

from decimal import Decimal

from .model import CycleState


def status_text(state: CycleState) -> str:
    return (
        f"active={state.active}, armed={state.armed}, phase={state.phase}, "
        f"scenario={state.scenario}, recovery={state.recovery}, "
        f"paused={state.paused}, manual={state.manual}, "
        f"cycle_target={state.cycle_target_profit}, "
        f"profit200={state.profit_override}, remaining={state.profit_override_remaining}"
    )


def scenario_nine_result_text(state: CycleState, long_fill: Decimal, short_fill: Decimal) -> str:
    return (
        "🏁 Сценарий 9 завершён автоматически\n"
        f"Фактическое закрытие LONG: {long_fill}\n"
        f"Фактическое закрытие SHORT: {short_fill}\n"
        f"Разница закрытий: {state.scenario_nine_close_gap} пункта\n"
        f"Накопленные убытки сценариев 1–8: {state.scenario_nine_prior_losses} пункта\n"
        f"Дополнительный убыток исполненных trigger: {state.scenario_nine_extra_loss} пункта\n"
        f"Итоговый убыток сценария 9: {state.scenario_nine_total_loss} пункта\n"
        f"Trigger-ордера текущего цикла отменены и проверены: "
        f"{'ДА' if state.scenario_nine_triggers_verified else 'НЕТ'}\n"
        "Обе позиции закрыты. Автоматика продолжит обычные циклы."
    )


def pnl_text(state: CycleState, positions: list[dict], transactions: list[dict]) -> str:
    unrealized = sum((_decimal(_position(item).get("upl")) for item in positions), Decimal("0"))
    realized = sum((_decimal(_value(item, "profitAndLoss", "pnl", "amount", "size"))
                    for item in transactions), Decimal("0"))
    currencies = {_value(item, "currency") for item in positions + transactions if _value(item, "currency")}
    currency = ",".join(sorted(str(value) for value in currencies)) or "account currency"
    return (
        f"Сценарий: {state.scenario}\nRecovery: {state.recovery}\n"
        f"Закрытый P&L за период истории: {realized} {currency}\n"
        f"Нереализованный P&L: {unrealized} {currency}\n"
        f"Суммарно: {realized + unrealized} {currency}"
    )


def cycle_result_text(state: CycleState, direction: str, fill: Decimal, size: Decimal) -> str:
    """Explain the completed cycle without depending on broker history latency."""
    gross_money = state.gross_take_profit * size
    losses_money = state.realized_losses * size
    net_money = state.net_cycle_result * size
    return (
        f"🏁 Итог завершённого цикла\n"
        f"TP сторона: {direction}\nФактическое закрытие: {fill}\n"
        f"Валовая прибыль TP: {state.gross_take_profit} пункта\n"
        f"Общие убытки закрытых сторон: {state.realized_losses} пункта\n"
        f"Итог цикла: {state.net_cycle_result} пункта\n"
        f"Размер каждой позиции: {size}\n"
        f"Расчёт по размеру: прибыль {gross_money}; убытки {losses_money}; итог {net_money}\n"
        "Точная сумма в валюте счёта берётся из Capital.com командой /pnl."
    )


def _position(item: dict) -> dict:
    return item.get("position", item)


def _value(item: dict, *keys: str):
    containers = [item, item.get("position", {}), item.get("transaction", {})]
    return next((container[key] for key in keys for container in containers
                 if isinstance(container, dict) and container.get(key) is not None), None)


def _decimal(value) -> Decimal:
    try:
        return Decimal(str(value or "0").replace(",", ""))
    except Exception:
        return Decimal("0")
