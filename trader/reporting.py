from __future__ import annotations

from decimal import Decimal

from .model import CycleState, Leg


def cycle_heading(state: CycleState, event: str, *, next_scenario: int | None = None) -> str:
    transition = f" → следующий сценарий {next_scenario}" if next_scenario is not None else ""
    return (
        f"Цикл №{state.cycle_id or state.diagnostic_cycle_number or '-'}; "
        f"попытка {state.cycle_attempt or '-'}; сценарий {state.scenario}{transition}\n"
        f"Событие: {event}"
    )


def recovery_snapshot(state: CycleState) -> dict[str, dict[str, Decimal | str | bool | None]]:
    result = {}
    for leg in (state.long, state.short):
        if leg:
            result[leg.direction] = {
                "open": leg.open, "size": leg.size, "entry": leg.current_entry,
                "trigger": leg.original_trigger_level, "distance": leg.stop_distance,
                "base": leg.recovery, "temp_stop": leg.temporary_stop_compensation,
                "temp_spread": leg.temporary_spread_compensation,
                "temp_slippage": leg.temporary_slippage_compensation,
                "effective": leg.effective_recovery, "stop": leg.stop,
                "target": leg.take_profit, "trigger_id": leg.trigger_id,
            }
    return result


def leg_details(leg: Leg, *, broker_stop=None, broker_target=None) -> str:
    if leg.open:
        status = "ОТКРЫТА (подтверждённый dealId)" if leg.deal_id else "ГОТОВИТСЯ/УТОЧНЯЕТСЯ"
    elif leg.trigger_id:
        status = f"ЗАКРЫТА; Trigger ожидает исполнения ({leg.trigger_id})"
    else:
        status = "ЗАКРЫТА; следующий Trigger ещё не подтверждён"
    return (
        f"{leg.direction}: {status}\n"
        f"  объём={leg.size}; current_entry={leg.current_entry}; "
        f"original_trigger={leg.original_trigger_level}\n"
        f"  SL distance={leg.stop_distance}; основной Recovery={leg.recovery}\n"
        f"  temporary: SL={leg.temporary_stop_compensation} + spread="
        f"{leg.temporary_spread_compensation} + slippage={leg.temporary_slippage_compensation} "
        f"= {leg.temporary_recovery}\n"
        f"  эффективный Recovery для TP={leg.effective_recovery}\n"
        f"  расчётные SL/TP={leg.stop} / {leg.take_profit}\n"
        f"  брокер подтвердил SL/TP="
        f"{broker_stop if broker_stop is not None else 'не подтверждено'} / "
        f"{broker_target if broker_target is not None else 'не подтверждено'}"
    )


def recovery_change_text(
    state: CycleState, before: dict, *, event: str, direction: str,
    stop_slippage: Decimal | None = None, trigger_slippage: Decimal | None = None,
) -> str:
    lines = [cycle_heading(state, event), "", "Изменение Recovery (ценовые расстояния, не P&L):"]
    for leg in (state.long, state.short):
        if not leg:
            continue
        old = before.get(leg.direction, {})
        lines.extend([
            f"{leg.direction}:",
            f"  основной до={old.get('base', 'не подтверждено')}",
        ])
        if leg.direction == direction and stop_slippage is not None:
            lines.append(f"  SL slippage=|плановый SL − fill|={stop_slippage}")
        if leg.direction == direction and trigger_slippage is not None:
            old_size = old.get("size", leg.size)
            if old_size != leg.size:
                lines.append(
                    f"  объём {old_size} → {leg.size}; накопление пересчитано с коэффициентом "
                    f"{leg.size / old_size}"
                )
            lines.append(f"  Trigger slippage=|original trigger − fill|={trigger_slippage}")
        lines.extend([
            f"  основной после={leg.recovery}",
            f"  temporary после: SL={leg.temporary_stop_compensation} + spread="
            f"{leg.temporary_spread_compensation} + slippage="
            f"{leg.temporary_slippage_compensation} = {leg.temporary_recovery}",
            f"  эффективный Recovery={leg.recovery} + {leg.temporary_recovery} "
            f"= {leg.effective_recovery}",
            f"  расчётные SL/TP={leg.stop} / {leg.take_profit}",
        ])
    return "\n".join(lines)


def status_text(state: CycleState) -> str:
    legs = ", ".join(
        f"{leg.direction}(size={leg.size}, sl_distance={leg.stop_distance}, "
        f"recovery={leg.recovery}, temporary="
        f"{leg.temporary_recovery})"
        for leg in (state.long, state.short) if leg
    ) or "-"
    return (
        f"active={state.active}, armed={state.armed}, phase={state.phase}, "
        f"scenario={state.scenario}, recovery={state.recovery}, "
        f"paused={state.paused}, manual={state.manual}, "
        f"attempt={state.active_attempt_id or '-'}, attempts_total={state.attempt_counter}, "
        f"cycle_id={state.cycle_id or '-'}, cycle_attempt={state.cycle_attempt or '-'}, "
        f"continuation_until={state.continuation_pause_until or '-'}, "
        f"continuation_blocked={state.continuation_stopped_by_user}, "
        f"continuation_owner={state.continuation_managed}, "
        f"continuation_stage={state.continuation_stage or '-'}, "
        f"completed_cycles={state.completed_cycles}, all_attempts_result={state.attempt_result_total}, "
        f"attempt_statistics={'УТОЧНЯЕТСЯ' if state.pending_actual_attempt_id else 'ПОЛНАЯ'}, "
        f"cycle_target={state.cycle_target_profit}, "
        f"profit200={state.profit_override}, remaining={state.profit_override_remaining}, "
        f"legs={legs}"
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
        f"Суммарно: {realized + unrealized} {currency} (по истории брокера)\n"
        f"Сохранённый результат всех торговых попыток: {state.attempt_result_total}\n"
        f"Полнота статистики: "
        f"{'УТОЧНЯЕТСЯ по попытке ' + str(state.pending_actual_attempt_id) if state.pending_actual_attempt_id else 'ПОЛНАЯ'}\n"
        f"Завершённых полных циклов: {state.completed_cycles}; "
        f"создано попыток: {state.attempt_counter}"
    )


def cycle_result_text(state: CycleState, direction: str, fill: Decimal, size: Decimal) -> str:
    """Explain the completed cycle without depending on broker history latency."""
    winner = state.long if direction == "BUY" else state.short
    winner_size = winner.size if winner and winner.size else size
    gross_money = state.gross_take_profit * winner_size
    losses_money = state.realized_loss_money or state.realized_losses * size
    net_money = state.net_cycle_money or gross_money - losses_money
    return (
        f"🏁 Итог завершённого цикла\n"
        f"TP сторона: {direction}\nФактическое закрытие: {fill}\n"
        f"Валовая прибыль TP: {state.gross_take_profit} пункта\n"
        f"Общие убытки закрытых сторон: {state.realized_losses} пункта\n"
        f"Итог цикла: {state.net_cycle_result} пункта\n"
        f"Размер TP позиции: {winner_size}\n"
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
