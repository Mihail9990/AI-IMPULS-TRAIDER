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
