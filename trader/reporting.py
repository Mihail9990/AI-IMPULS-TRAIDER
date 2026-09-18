from __future__ import annotations

from decimal import Decimal

from .model import CycleState, Leg

D = Decimal


def cycle_heading(state: CycleState, event: str, *, next_scenario: int | None = None) -> str:
    transition = f" → следующий сценарий {next_scenario}" if next_scenario is not None else ""
    return (
        f"Цикл №{state.cycle_id or state.diagnostic_cycle_number or '-'}; "
        f"попытка {state.cycle_attempt or '-'}; сценарий {state.scenario}{transition}\n"
        f"Событие: {event}"
    )


def recovery_snapshot(state: CycleState) -> dict:
    result = {"_general": {
        "general_recovery": state.general_recovery,
        "target_value": state.target_value,
        "event_count": len(state.recovery_events),
    }}
    for leg in (state.long, state.short):
        if leg:
            result[leg.direction] = {
                "open": leg.open, "size": leg.size, "entry": leg.current_entry,
                "trigger": leg.original_trigger_level, "distance": leg.stop_distance,
                "recovery_distance": (state.general_recovery / leg.size if leg.size > 0 else None),
                "stop": leg.stop, "target": leg.take_profit, "trigger_id": leg.trigger_id,
            }
    return result


def leg_details(
    leg: Leg, *, broker_stop=None, broker_target=None, confirmation: str | None = None,
    readback: str | None = None, general_recovery: Decimal | None = None,
) -> str:
    if broker_stop is None:
        broker_stop = leg.confirmed_stop
    if broker_target is None:
        broker_target = leg.confirmed_take_profit
    status = ("ОТКРЫТА (подтверждённый dealId)" if leg.open and leg.deal_id else
              "ГОТОВИТСЯ/УТОЧНЯЕТСЯ" if leg.open else
              f"ЗАКРЫТА; Trigger={leg.trigger_id}" if leg.trigger_id else "ЗАКРЫТА")
    confirmation = confirmation if confirmation is not None else (leg.protection_confirmation or "не отправлено")
    readback = readback if readback is not None else (leg.protection_readback or "не выполнено")
    distance = (general_recovery / leg.size if general_recovery is not None and leg.size > 0
                else leg.recovery if leg.size > 0 else None)
    levels = (f"  расчётные SL/TP={leg.stop} / {leg.take_profit}\n"
              f"  отправленные SL/TP={leg.protection_sent_stop} / {leg.protection_sent_take_profit}\n"
              f"  confirmation={confirmation}; принятые SL/TP={leg.confirmation_stop} / {leg.confirmation_take_profit}\n"
              f"  повторное чтение /positions={readback}; последние сохранённые read-back SL/TP="
              f"{broker_stop} / {broker_target}"
              if leg.open else
              f"  исторические последние SL/TP={leg.stop} / {leg.take_profit}; "
              "не являются расчётом от нового Recovery")
    return (
        f"{leg.direction}: {status}\n"
        f"  dealId={leg.deal_id or 'не подтверждён'}; size={leg.size}; "
        f"current_entry={leg.current_entry}; original_trigger={leg.original_trigger_level}\n"
        f"  действующий D={leg.stop_distance}; recovery_distance={distance}\n{levels}\n"
        f"  формула TP: entry {'+' if leg.direction == 'BUY' else '−'} D "
        f"{'+' if leg.direction == 'BUY' else '−'} GENERAL_RECOVERY/size"
    )


def recovery_change_text(
    state: CycleState, before: dict, *, event: str, direction: str,
    stop_slippage: Decimal | None = None, trigger_slippage: Decimal | None = None,
) -> str:
    old_general = before.get("_general", {}).get("general_recovery", "?")
    last = state.recovery_events[-1] if state.recovery_events else {}
    lines = [
        cycle_heading(state, event), "", "Изменение денежного GENERAL_RECOVERY:",
        f"ДО={old_general}; событие={last.get('kind', 'без нового компонента')}; "
        f"добавка={last.get('amount', '0')}; ПОСЛЕ={state.general_recovery}",
        f"target_value={state.target_value}; фактический P&L учитывается отдельно.",
    ]
    for leg in (state.long, state.short):
        if not leg:
            continue
        distance = state.general_recovery / leg.size if leg.size > 0 else "НЕИЗВЕСТНО"
        lines.append(
            f"{leg.direction}: size={leg.size}; D={leg.stop_distance}; "
            f"recovery_distance={state.general_recovery}/{leg.size}={distance}; "
            f"entry={leg.current_entry}; SL={leg.stop}; TP={leg.take_profit}"
        )
    pending = [item for item in state.pending_recovery if not item.get("d_accounted")]
    for item in pending:
        lines.append(
            f"pending D: dealId={item.get('deal_id')}; {item.get('stop_distance')} × "
            f"{item.get('size')} = {item.get('pending_d_value')} денег; ещё не перенесён"
        )
    return "\n".join(lines)


def status_text(state: CycleState) -> str:
    legs = ", ".join(
        f"{leg.direction}(size={leg.size}, sl_distance={leg.stop_distance}, "
        f"recovery_distance="
        f"{state.general_recovery / leg.size if leg.size > 0 else 'НЕИЗВЕСТНО'})"
        for leg in (state.long, state.short) if leg
    ) or "-"
    return (
        f"active={state.active}, armed={state.armed}, phase={state.phase}, "
        f"scenario={state.scenario}, GENERAL_RECOVERY={state.general_recovery}, "
        f"paused={state.paused}, manual={state.manual}, "
        f"attempt={state.active_attempt_id or '-'}, attempts_total={state.attempt_counter}, "
        f"cycle_id={state.cycle_id or '-'}, cycle_attempt={state.cycle_attempt or '-'}, "
        f"continuation_until={state.continuation_pause_until or '-'}, "
        f"continuation_blocked={state.continuation_stopped_by_user}, "
        f"continuation_owner={state.continuation_managed}, "
        f"continuation_stage={state.continuation_stage or '-'}, "
        f"completed_cycles={state.completed_cycles}, all_attempts_result={state.attempt_result_total}, "
        f"attempt_statistics={'УТОЧНЯЕТСЯ' if state.pending_actual_attempt_id else 'ПОЛНАЯ'}, "
        f"cycle_target_distance={state.cycle_target_profit}, target_value={state.target_value}, "
        f"profit200={state.profit_override}, remaining={state.profit_override_remaining}, "
        f"legs={legs}"
    )


def scenario_nine_result_text(state: CycleState, long_fill: Decimal, short_fill: Decimal) -> str:
    deals = []
    calculated_money = D("0")
    for item in state.deal_history:
        if item.get("deal_id") not in state.attempt_deal_ids or item.get("close_level") is None:
            continue
        entry, close, size = (_decimal(item.get("entry")), _decimal(item.get("close_level")),
                              _decimal(item.get("size")))
        value = ((close - entry) if item.get("direction") == "BUY" else (entry - close)) * size
        calculated_money += value
        deals.append(
            f"• {item.get('direction')} dealId={item.get('deal_id')}; size={size}; "
            f"entry={entry}; close={close}; причина={item.get('close_source')}; "
            f"результат={value}"
        )
    detail = "\n".join(deals) or "• Broker history сделок ещё уточняется."
    return (
        "🏁 Сценарий 9 завершён автоматически\n"
        f"Цикл №{state.cycle_id or '-'}; попытка {state.cycle_attempt or '-'}; сценарий 9\n"
        f"Сделки:\n{detail}\n"
        f"Фактическое закрытие LONG: {long_fill}\n"
        f"Фактическое закрытие SHORT: {short_fill}\n"
        f"Разница закрытий: {state.scenario_nine_close_gap} пункта\n"
        f"Накопленные убытки сценариев 1–8: {state.scenario_nine_prior_losses} пункта\n"
        f"Дополнительный убыток исполненных trigger: {state.scenario_nine_extra_loss} пункта\n"
        f"Итоговый убыток сценария 9: {state.scenario_nine_total_loss} пункта\n"
        f"Trigger-ордера текущего цикла отменены и проверены: "
        f"{'ДА' if state.scenario_nine_triggers_verified else 'НЕТ'}\n"
        f"Расчёт по фактическим entry/close и size доступных сделок: {calculated_money}. "
        "Broker P&L в валюте счёта уточняется отдельно.\n"
        "Обе позиции закрыты. Автоматика продолжит обычные циклы."
    )


def pnl_text(state: CycleState, positions: list[dict], transactions: list[dict]) -> str:
    unrealized = sum((_decimal(_position(item).get("upl")) for item in positions), Decimal("0"))
    realized = sum((_decimal(_value(item, "profitAndLoss", "pnl", "amount", "size"))
                    for item in transactions), Decimal("0"))
    currencies = {_value(item, "currency") for item in positions + transactions if _value(item, "currency")}
    currency = ",".join(sorted(str(value) for value in currencies)) or "account currency"
    return (
        f"Сценарий: {state.scenario}\nGENERAL_RECOVERY: {state.general_recovery} денег\n"
        f"target_value: {state.target_value} денег\n"
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
    deals = []
    calculated_losses = D("0")
    cycle_records = [item for item in state.deal_history
                     if item.get("cycle_id") == state.cycle_id]
    selected = [item for item in (cycle_records or state.deal_history)
                if item.get("close_level") is not None
                and (cycle_records or item.get("deal_id") in state.attempt_deal_ids)]
    for item in selected:
        entry = _decimal(item.get("entry"))
        close = _decimal(item.get("close_level"))
        deal_size = _decimal(item.get("size"))
        result = ((close - entry) if item.get("direction") == "BUY" else (entry - close)) * deal_size
        if result < 0:
            calculated_losses += -result
        formula = (f"({close} − {entry}) × {deal_size}" if item.get("direction") == "BUY" else
                   f"({entry} − {close}) × {deal_size}")
        deals.append(
            f"• {item.get('direction', '?')} dealId={item.get('deal_id')}; объём={deal_size}; "
            f"entry={entry}; close={close}; причина={item.get('close_source') or '?'}; "
            f"результат={formula}={result}"
        )
    detail_complete = bool(selected) and calculated_losses == state.realized_loss_money
    # The durable aggregate spans every continuation attempt. Never replace it with a smaller
    # subtotal merely because broker/deal detail is incomplete.
    if not state.realized_loss_money and selected:
        losses_money = calculated_losses
    net_money = state.net_cycle_money if state.net_cycle_money else gross_money - losses_money
    detail = "\n".join(deals) or "• Детализация сделок ещё уточняется по broker history."
    attempts = [item for item in state.attempt_history if item.get("cycle_id") == state.cycle_id]
    attempt_detail = "\n".join(
        f"• попытка {item.get('cycle_attempt', '?')}: {item.get('status')} = {item.get('result')}"
        for item in attempts
    ) or "• Отдельные итоги попыток отсутствуют в сохранённом состоянии."
    return (
        f"🏁 Итог завершённого цикла\n"
        f"Сделки:\n{detail}\n"
        f"Полнота детализации: {'ПОЛНАЯ' if detail_complete else 'НЕПОЛНАЯ; денежный итог взят из полного сохранённого агрегата'}\n"
        f"Попытки логического цикла:\n{attempt_detail}\n"
        f"TP сторона: {direction}\nФактическое закрытие: {fill}\n"
        f"Валовая прибыль TP: {state.gross_take_profit} пункта\n"
        f"Общие убытки закрытых сторон: {state.realized_losses} пункта\n"
        f"Итог цикла: {state.net_cycle_result} пункта\n"
        f"Размер TP позиции: {winner_size}\n"
        f"Расчёт по размеру: прибыль {gross_money}; убытки {losses_money}; итог {net_money}\n"
        f"Судьба оставшегося Trigger: {state.last_trigger_resolution}\n"
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
