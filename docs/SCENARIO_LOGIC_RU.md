# Фактическая денежная модель сценариев 1–9 (version 3)

## Термины и единый ledger

`DISTANCE_SCENARIO` — настроенная stop-геометрия текущего сценария. `D_VALUE` — стоимость
фактически действовавшей stop-distance закрытой позиции:

```text
D_VALUE = effective_stop_distance_at_close * actual_closed_size
```

`CycleState.general_recovery` — единый денежный стратегический ledger логического цикла, а не
broker P&L. Он никогда не масштабируется при смене size. Size используется только в стоимости
конкретного события и при переводе денежного Recovery в расстояние TP.

`deal_history` хранит отдельную запись каждого permanent position `dealId`, а
`scenario_transitions` — подтверждённую последовательность переоткрытий. Поэтому несколько
последовательных reentry одной стороны не переписывают предыдущую сделку: Scenario открытия,
Scenario закрытия и действовавшая stop-distance остаются независимыми фактами. У перехода
сохраняются owned `workingOrderId` и broker time `WORKING_ORDER/EXECUTED`; неизвестная
принадлежность старой записи не подменяется текущими `cycle_id/cycle_attempt`.

## Initial Scenario 1

После двух подтверждённых fills одинакового фактического размера один раз фиксируются:

```text
spread_value = abs(BUY_fill - SELL_fill) * initial_actual_size
target_value = cycle_target_profit * initial_actual_size
GENERAL_RECOVERY = spread_value + target_value
```

Quotes до fills ничего не начисляют. Target больше не добавляется ни при reentry, ни при
continuation pair.

Для Scenario 1 действует специальная TP-формула:

```text
BUY_TP  = current_entry + DISTANCE_SCENARIO + GENERAL_RECOVERY / actual_size
SELL_TP = current_entry - DISTANCE_SCENARIO - GENERAL_RECOVERY / actual_size
```

При SL S1 сразу добавляется только
`abs(effective_broker_SL - actual_fill) * actual_closed_size`. `D_VALUE` сохраняется в durable
closure snapshot с `d_accounted=false`. Если survivor раньше Trigger достигает TP, cycle
завершается и этот pending D не переносится.

## Linked closure и reentry

Каждый confirmed SL сохраняет stable identity, direction, broker chronology, `scenario_at_close`,
entry/fill/size, effective stop/distance, slippage, `D_VALUE`, original Trigger anchor и два
независимых флага `d_accounted`/`reentry_accounted`.

Для любого confirmed Trigger либо MARKET reentry:

```text
D_TO_ADD = closure.D_VALUE if not closure.d_accounted else 0
TRIGGER_SLIPPAGE_VALUE =
    abs(closure.original_trigger_anchor - actual_reentry_fill) * actual_new_size
GENERAL_RECOVERY += D_TO_ADD + TRIGGER_SLIPPAGE_VALUE
```

Критерий — связанный closure snapshot, а не текущий номер Scenario. Поэтому поздно найденный S1
closure не теряет D даже после локального перехода к S2. D и Trigger slippage имеют независимые
stable recovery-event keys. Replay не меняет ledger и не повторяет Scenario transition.

`original_trigger_level` неизменяем в пределах attempt; `current_entry` заменяется actual fill.

Историческая связь исполнения сохраняет owned working-order ID, permanent position dealId и broker
UTC timestamp `WORKING_ORDER/EXECUTED`. Она не удаляется при очистке активного `leg.trigger_id` после
reentry и используется для позднего SL после restart. `position.createdDateUTC` является временем
публикации/создания позиции и не подменяет broker execution chronology.
Broker execution chronology, а не arrival order REST/history, определяет `scenario_at_close`.
Неоднозначная chronology блокируется reconciliation/manual без приблизительного D.
Полное подтверждённое close evidence (fill, actual size, source/status/type, broker time и
исторические confirmed SL/TP) сохраняется до конца логического цикла. Пустой последующий history
response его не стирает. Существенно противоречащая запись блокируется **до** accounting;
подтверждённые partial fills сохраняются раздельно, используют собственный actual closed size и
оставляют остаток позиции открытым.

## Scenario 2–8

При confirmed SL немедленно начисляются два независимых компонента:

```text
D_VALUE = effective_stop_distance_at_close * actual_closed_size
SL_SLIPPAGE_VALUE = abs(effective_broker_SL - actual_SL_fill) * actual_closed_size
GENERAL_RECOVERY += D_VALUE + SL_SLIPPAGE_VALUE
```

Closure остаётся pending для reentry, но уже имеет `d_accounted=true`. Поэтому обычный S2–S8
reentry добавляет только Trigger slippage. Scenario увеличивается лишь после actual confirmed fill.
Survivor сохраняет dealId, current entry и actual size; новый `DISTANCE_SCENARIO` применяется к
обеим открытым legs, но size одной стороны не масштабирует другую.

TP для каждой открытой leg рассчитывается независимо:

```text
BASE_VALUE = DISTANCE_SCENARIO * actual_leg_size
REMAINING_RECOVERY = max(0, GENERAL_RECOVERY - BASE_VALUE)
BUY_TP  = current_entry + DISTANCE_SCENARIO + REMAINING_RECOVERY / actual_leg_size
SELL_TP = current_entry - DISTANCE_SCENARIO - REMAINING_RECOVERY / actual_leg_size
TP_PROFIT_VALUE = max(GENERAL_RECOVERY, BASE_VALUE)
```

Вычисление TP и `refresh_targets()` не уменьшают и вообще не изменяют ledger.

## Projection, double-SL и continuation

Projection использует тот же linked closure:

```text
projected_GENERAL = stored_GENERAL + (closure.D_VALUE if not closure.d_accounted else 0)
```

Она не меняет flags, events, Scenario или stored GENERAL и не придумывает future slippage.
После broker-flat double-SL S1 pending D переносится один раз. В S2–S8 D уже учтён на SL и снова
не добавляется; судьба Trigger/reentry при этом всё равно должна быть разрешена отдельно.

`CycleContinuation` сохраняет exclusive ownership, reconciliation, паузу 300 секунд, filter,
preflight и pair formation. Только две actual equal-size fills добавляют ровно один раз:

```text
continuation_spread_value = abs(BUY_fill - SELL_fill) * actual_pair_size
```

Target не повторяется, а actual fills новой pair становятся anchors новой attempt.

## TP/Trigger race и граница логического цикла

Если owned Trigger исполнился в гонке отмены после подтверждённого TP survivor, появившаяся
позиция закрывается отдельно по permanent dealId. Её signed результат рассчитывается по actual
entry, actual close и actual size, показывается отдельной строкой и не меняет GENERAL_RECOVERY,
основные realized losses или стратегический итог TP. Неизвестный исход close сохраняется и
сверяется после restart без повторного DELETE.

После окончательного завершения сначала формируется самодостаточный Telegram-report и в
diagnostic log записывается `CYCLE_LEDGER_FINAL` со сделками, attempts, Recovery events,
переходами, race results и доступным transaction snapshot. Только затем detailed working ledger
и transaction jobs этого `cycle_id` очищаются. Агрегаты/counters и durable outbox сохраняются;
late worker result с удалённым key/generation не может восстановить старую историю в новом цикле.

## Scenario 9, actual P&L и migration

S8 SL начисляет D + SL slippage; linked S8→S9 reentry обычно начисляет только Trigger slippage и
переводит state в `SCENARIO_9_CLOSING`. Ordinary Recovery TP и S10 отсутствуют. Существующие
Trigger cancellation/race, concurrent close, actual-fill и restart-защиты сохраняются.

Actual P&L независимо суммируется по broker entry, close, direction и собственному actual size
каждого deal; GENERAL_RECOVERY его не заменяет.

Подтверждённый close event сохраняется целиком вместе с effective protection. Диагностический
диапазон `SL/TP ± 0.50` вычисляется только после установления deal identity и не является основанием
для source, Scenario, accounting или mutation.

Новые циклы имеют `recovery_model_version=3`. Inactive old state начинает следующий cycle в v3.
Active v1/v2 не мигрируется приблизительно: если точная deterministic continuation не доказуема,
состояние сохраняется и automation безопасно блокируется в manual.
