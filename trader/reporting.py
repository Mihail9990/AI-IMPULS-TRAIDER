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


def leg_details(
    leg: Leg, *, broker_stop=None, broker_target=None, confirmation: str | None = None,
    readback: str | None = None,
) -> str:
    if broker_stop is None:
        broker_stop = leg.confirmed_stop
    if broker_target is None:
        broker_target = leg.confirmed_take_profit
    if leg.open:
        status = "ОТКРЫТА (подтверждённый dealId)" if leg.deal_id else "ГОТОВИТСЯ/УТОЧНЯЕТСЯ"
    elif leg.trigger_id:
        status = f"ЗАКРЫТА; Trigger ожидает исполнения ({leg.trigger_id})"
    else:
        status = "ЗАКРЫТА; следующий Trigger ещё не подтверждён"
    confirmation = confirmation if confirmation is not None else (
        leg.protection_confirmation or "не отправлено"
    )
    readback = readback if readback is not None else (
        leg.protection_readback or "не выполнено"
    )
    sent_stop, sent_tp = leg.protection_sent_stop, leg.protection_sent_take_profit
    accepted_stop, accepted_tp = leg.confirmation_stop, leg.confirmation_take_profit
    read_stop, read_tp = broker_stop, broker_target
    read_label = (
        "фактически прочитанные SL/TP"
        if str(readback).upper() == "ПОДТВЕРЖДЕНО" else
        "последние сохранённые read-back SL/TP (источник/ревизия не подтверждают новый расчёт)"
    )
    if leg.open:
        levels = (
            f"  расчётные SL/TP={leg.stop} / {leg.take_profit}\n"
            f"  отправленные SL/TP={sent_stop if sent_stop is not None else 'не отправлено'} / "
            f"{sent_tp if sent_tp is not None else 'не отправлено'}\n"
            f"  confirmation={confirmation}; принятые SL/TP="
            f"{accepted_stop if accepted_stop is not None else 'не подтверждено'} / "
            f"{accepted_tp if accepted_tp is not None else 'не подтверждено'}\n"
            f"  повторное чтение /positions={readback}; {read_label}="
            f"{read_stop if read_stop is not None else 'не подтверждено'} / "
            f"{read_tp if read_tp is not None else 'не подтверждено'}\n"
            f"  принадлежность: dealId={leg.deal_id or 'не подтверждён'}; "
            "старое подтверждение не подтверждает вновь рассчитанный уровень\n"
            f"  формула TP: {leg.current_entry} "
            f"{'+' if leg.direction == 'BUY' else '−'} {leg.stop_distance} "
            f"{'+' if leg.direction == 'BUY' else '−'} {leg.effective_recovery} "
            f"= {leg.take_profit}"
        )
    else:
        levels = (
            f"  исторические последние SL/TP={leg.stop} / {leg.take_profit}; "
            "позиция закрыта, эти уровни не являются расчётом от нового Recovery"
        )
    return (
        f"{leg.direction}: {status}\n"
        f"  dealId={leg.deal_id or 'ещё не подтверждён'}; объём={leg.size}; "
        f"current_entry={leg.current_entry}; "
        f"original_trigger={leg.original_trigger_level}\n"
        f"  SL distance={leg.stop_distance}; основной Recovery={leg.recovery}\n"
        f"  temporary: SL={leg.temporary_stop_compensation} + spread="
        f"{leg.temporary_spread_compensation} + slippage={leg.temporary_slippage_compensation} "
        f"= {leg.temporary_recovery}\n"
        f"  эффективный Recovery для TP={leg.effective_recovery}\n{levels}"
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
        old_base = old.get("base")
        old_temp = (old.get("temp_stop", D("0")) + old.get("temp_spread", D("0"))
                    + old.get("temp_slippage", D("0")))
        lines.extend([
            f"{leg.direction}:",
            f"  ДО: объём={old.get('size', '?')}; основной={old_base}; temporary: "
            f"SL={old.get('temp_stop', '?')} + spread={old.get('temp_spread', '?')} + "
            f"slippage={old.get('temp_slippage', '?')} = {old_temp}; "
            f"эффективный={old.get('effective', '?')}",
        ])
        if leg.direction == direction and stop_slippage is not None:
            old_size = old.get("size", leg.size)
            other = state.short if leg.direction == "BUY" else state.long
            if other and old_size != other.size:
                weighted = stop_slippage * old_size / other.size
                lines.append(
                    f"  SL slippage закрытой стороны={stop_slippage}; для survivor объёмов "
                    f"{old_size}/{other.size}: {stop_slippage} × {old_size} / {other.size} "
                    f"= {weighted}"
                )
            else:
                lines.append(f"  SL slippage=|плановый SL − fill|={stop_slippage}")
        elif stop_slippage is not None:
            closed = before.get(direction, {})
            closed_size = closed.get("size", leg.size)
            weighted = stop_slippage * closed_size / leg.size
            lines.append(
                f"  пересчёт SL slippage закрытой {direction} для этой стороны: "
                f"{stop_slippage} × {closed_size} / {leg.size} = {weighted}; "
                f"основной {old_base} → {leg.recovery}"
            )
        if leg.direction == direction and trigger_slippage is not None:
            old_size = old.get("size", leg.size)
            other_old = before.get("SELL" if leg.direction == "BUY" else "BUY", {})
            if leg.size == old_size * 2:
                if other_old.get("size") == leg.size:
                    lines.extend([
                        f"  второе увеличение {old_size} → {leg.size}: temporary до={old_temp} "
                        "удаляется перед выравниванием",
                        f"  ({old.get('effective')} − {old_temp}) / 2 + "
                        f"{leg.stop_distance} + {trigger_slippage} = {leg.recovery}",
                        "  объёмы сравнялись: основной Recovery противоположной стороны "
                        f"синхронизирован до {leg.recovery}; все temporary обнулены",
                    ])
                else:
                    lines.append(
                        f"  первое увеличение {old_size} → {leg.size}: "
                        f"({old_base} + {leg.stop_distance}) / 2 + {trigger_slippage} "
                        f"= {leg.recovery}"
                    )
            else:
                lines.append(
                    f"  объём остаётся {leg.size}: {old_base} + новая SL distance "
                    f"{leg.stop_distance} + Trigger slippage {trigger_slippage} = {leg.recovery}"
                )
        elif trigger_slippage is not None:
            changed = state.long if direction == "BUY" else state.short
            changed_old = before.get(direction, {})
            if changed and changed.size == changed_old.get("size", changed.size) * 2 \
                    and old.get("size") == changed.size:
                lines.append(
                    f"  объёмы сравнялись с {direction}: основной синхронизирован "
                    f"{old_base} → {leg.recovery}; temporary {old_temp} → 0"
                )
            else:
                base_delta = leg.recovery - old_base
                lines.append(
                    f"  компенсация переоткрытия {direction}: основной {old_base} + "
                    f"{base_delta} = {leg.recovery}; изменения temporary: "
                    f"SL {old.get('temp_stop', 0)} → {leg.temporary_stop_compensation}, "
                    f"spread {old.get('temp_spread', 0)} → {leg.temporary_spread_compensation}, "
                    f"slippage {old.get('temp_slippage', 0)} → "
                    f"{leg.temporary_slippage_compensation}"
                )
        lines.extend([
            f"  ПОСЛЕ: основной={leg.recovery}",
            f"  temporary: SL={leg.temporary_stop_compensation} + spread="
            f"{leg.temporary_spread_compensation} + slippage="
            f"{leg.temporary_slippage_compensation} = {leg.temporary_recovery}",
            f"  эффективный Recovery={leg.recovery} + {leg.temporary_recovery} "
            f"= {leg.effective_recovery}",
            (f"  собственный TP: {leg.current_entry} "
             f"{'+' if leg.direction == 'BUY' else '−'} {leg.stop_distance} "
             f"{'+' if leg.direction == 'BUY' else '−'} {leg.effective_recovery} "
             f"= {leg.take_profit}" if leg.open else
             f"  позиция закрыта; последние SL/TP {leg.stop}/{leg.take_profit} исторические"),
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
    deals = []
    calculated_losses = D("0")
    selected = [item for item in state.deal_history
                if item.get("deal_id") in state.attempt_deal_ids and item.get("close_level") is not None]
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
    if selected:
        losses_money = calculated_losses
        net_money = gross_money - losses_money
    detail = "\n".join(deals) or "• Детализация сделок ещё уточняется по broker history."
    return (
        f"🏁 Итог завершённого цикла\n"
        f"Сделки:\n{detail}\n"
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
