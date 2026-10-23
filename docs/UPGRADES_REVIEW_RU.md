# Независимый review offline апгрейдов

Reviewer: `/root/independent_review`, read-only. Native сервисы и HTTP сервер не запускал.

Исправлены подтверждённые замечания:

1. Третье касание после pinch создавало случайный preview — multi-touch сохраняется до отпускания всех указателей.
2. Tap существующей BUY стороны показывал create, но заменял уровень — теперь move с old price.
3. Исторический IN блокировал замену после закрытия — entries/opened_ids очищаются только после подтверждённого fullclose.
4. TP пользователя сбрасывался после readback — сохранён до явного изменения N.
5. Сбой между /start saves мог включить auto вместо semi — одна запись mode/phase/offset.
6. Manual close с нулевым долгом оставлял пустой recovery — цикл архивируется, новые входы ждут команды.
7. Fake CLOSE мог закрыть первую позицию вместо указанной — exact ID; неизвестные owned позиции блокируют sync dispatch.
8. REJECTED MODIFY/CLOSE мог повторяться каждый tick — окончательный отказ останавливает; retry только доказанный never-sent и свежая разрешающая проверка.
9. Доказанный validation refusal становился UNKNOWN — явный ActionRejected до действий.
10. HTTP502 мог очистить pending request — сохранение ID до receipt readback, без нового POST.
11. Медленный HTTP клиент блокировал owner tick — отдельный transport thread, owner mailbox, SQLite только owner.
12. Пересоздание/вытеснение transport cache теряло durable receipts — merge с приоритетом durable UNKNOWN над конфликтующим payload.
13. Settings mode/modules применялись частично при позднем отказе — полная prevalidation и одна запись при смене режима.

Финальное заключение reviewer: «подтверждённых неисправленных дефектов в проверенном diff нет».
Независимый полный offline runner: 466 tests OK, сетевых обращений 0.
Независимо проверено 17 Chromium сценариев. Основной агент затем выполнил 18 UI tests,
включая добавленную end-to-end fake проверку N Apply.

Ограничение review: новый торговый контур simulator-only. Native engine/adapters не
изменены; review не является подтверждением готовности production-интеграции.

## Native integration — последующий review

Предыдущее ограничение simulator-only выше относится только к commit 8a2de9e.
В новом diff runtime, адаптер, ledger и feed интегрированы; real execution не запускался.
Reviewer `/root/independent_review` проверил изменения read-only и воспроизвёл:

1. Settings до первого tick могли перевести сохранённый legacy cycle в fixed с нулевым долгом — metadata legacy теперь фиксируется при загрузке.
2. RECOVERY_TARGET CLOSE мог уйти после движения цены в убыток между check/send — target/Bid/Ask/profit повторно проверяются при каждой пересборке request, false условие = доказанный not sent.
3. Старый preview мог получить scope новой БД/счёта — captured scope сверяется, сервер scope включает durable store UUID; pending receipt тоже привязан к scope.
4. Истёкшая session давала 401 и оставляла вечный UNKNOWN UI — 401 является доказанным not queued, после login можно подтвердить новую команду.
5. Fresh tick ошибочно делал будущие/устаревшие свечи FRESH — отдельная проверка временного окна свечей и статусы.
6. Legacy reset не понимал CLOSE; fixed наследовал семантику списания — legacy доказательства расширены, fixed reset явно запрещён до ответа пользователя, debt сохраняется.

Независимо прошли 482 backend tests (0 network) и 21 Chromium test.
После этого добавлены ещё 6 native regressions (replace/fill race/unknown cancel,
time latch cancel/restart/protection, fixed reset guard); полный основной run 488 OK.
Заключительный независимый review: 488 backend tests OK, network 0; ещё раз 3 native Chromium tests OK. Новых функциональных дефектов не обнаружено.
Оставшееся ограничение — только ожидающая семантика fixed emergency reset и
отдельные реальные MT5/VPS проверки, которые запрещены текущим заданием.

Финальная дополнительная auth regression: не-ASCII неверный пароль возвращает AUTH_FAILED, а не исключение транспорта; полный основной run 489 OK, network 0.

## Fixed /reset_manual: подтверждённая семантика реализована

Новый review снял прежний blocker reset. Выявлены и исправлены два сценария:
1. Crash после auto new_cycle_snapshot мог оставить carry вне legacy-цикла: metadata теперь входит в первый атомарный snapshot.
2. Двойной OCO fill и +10/-10 net давали ложный долг из-за промежуточного clamp: reset считает общий net batch один раз, сохраняет все individual deal/round identities.

Независимый полный runner: 495 tests OK, network0; основной полный runner — то же.
Полный Chromium набор: 21 tests OK. Confirmed flat proofs, UNKNOWN refusal, debt>0/0,
costs, commit rollback, restart/duplicate, time latch и отсутствие автоматического входа проверены.
Подтверждённых неисправленных дефектов review не обнаружил. Реальные сервисы не запускались.
