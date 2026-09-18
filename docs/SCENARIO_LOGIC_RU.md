# Фактическая денежная модель сценариев 1–9

## Единственный источник Recovery

`CycleState.general_recovery` — единый денежный баланс логического recovery-цикла. Его нельзя
масштабировать при изменении объёма. Для позиции с фактическим объёмом `size` используется
производное расстояние `general_recovery / size`; при нулевом или неизвестном размере автоматика
останавливается.

```text
BUY_SL  = current_entry - stop_distance
SELL_SL = current_entry + stop_distance
BUY_TP  = current_entry + stop_distance + general_recovery / size
SELL_TP = current_entry - stop_distance - general_recovery / size
```

Единая реализация формул находится в `trader/model.py:protection_levels`; переходы и однократный
денежный журнал — в `trader/engine.py:Strategy`.

## Начальная пара и денежная цель

После двух подтверждённых fills один раз фиксируются:

```text
target_value = cycle_target_profit * initial_position_size
spread_value = abs(BUY_fill - SELL_fill) * initial_position_size
general_recovery = target_value + spread_value
```

Предварительные котировки заменяются фактическими fills согласованно; повтор того же подтверждения
ничего не добавляет. `/profit200` влияет на `cycle_target_profit` только при начале цикла.

## SL, pending D и переоткрытие

При подтверждённом SL фактический P&L сразу учитывается отдельно. В GENERAL_RECOVERY немедленно
добавляется только `abs(confirmed_SL - close_fill) * closed_size`. Неизменяемый снимок содержит
`dealId`, entry, old size, действовавшие D/SL, close fill, оба представления slippage и
`pending_D_value = old_D * old_size`.

До исполнения Trigger сценарий и D survivor не меняются. После подтверждённого Trigger или
эквивалентного MARKET fallback ровно один раз добавляются:

```text
pending_D_value + abs(original_trigger_level - actual_reentry_fill) * new_actual_size
```

Затем сценарий увеличивается, новый D применяется к обеим фактически открытым позициям, но новый
объём и `current_entry` получает только переоткрытая сторона. Якорь `original_trigger_level` не
заменяется reentry fill. Это допускает любую последовательность BUY/SELL, включая повторные
переоткрытия одной стороны.

`projected_reopen()` использует `general_recovery + pending_D_value` только для предварительной
защиты. Проекция не изменяет баланс, pending-снимок или сценарий и не придумывает будущее
slippage.

## Double-SL и continuation

Перед признанием flat разрешаются связанные working orders, уже исполненные Trigger и pending
MARKET. После доказанного flat все неучтённые pending D попытки переносятся один раз. Пятиминутная
пауза, фильтр, preflight и формирование новой пары остаются под владельцем `CycleContinuation`.
Новая подтверждённая пара добавляет только `abs(BUY_fill-SELL_fill) * pair_size`; target повторно
не добавляется.

## Scenario 9 и фактический результат

Переход 8→9 использует ту же формулу pending D + Trigger slippage, после чего запускается
существующее специальное закрытие без Scenario 10. GENERAL_RECOVERY не является фактическим
убытком. P&L вычисляется по реальным entry, close и size; результаты попыток и всего логического
цикла сохраняются отдельно.

## Восстановление

Версия денежной модели и все денежные компоненты сохраняются в `bot_state.json`. Повтор REST,
history, confirmation, WebSocket или restart не применяет событие второй раз благодаря стабильным
ключам `recovery_events` и снимкам `pending_recovery`. Фоновый history worker остаётся read-only.
Активное состояние старой per-leg модели без достаточных денежных доказательств не конвертируется
приблизительно: оно сохраняется и блокируется в manual с объяснением.
