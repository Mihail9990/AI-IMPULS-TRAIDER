# Апгрейды: сохранённый offline этап

База: `9519208e09f680d003ce893e6dc8688870823d63`, ветка `upgrades/local-control`.
Защиты времени из этой базы сохранены. Это отдельный deliverable; исправленный
архив time-safety не заменяется. Весь новый торговый контур ограничен явным
`interactive_simulator_v1`; production runtime по-прежнему создаёт прежний Controller.
Отсутствие production-интеграции ниже означает отсутствующую реализацию/подключение,
а не только то, что реальные сделки не запускались.

## Матрица требований

| Требование | Что реализовано и проверено на fake | UI/mock | Что отсутствует в production |
|---|---|---|---|
| Auto/semi | InteractiveController: auto ждёт закрытую M1 прежней стратегии; semi ждёт команду, после полного цикла снова ждёт. Цены одного/двух triggers и два диапазона; BUY от Ask, SELL от Bid снимка получения команды | Выбор режима, Apply через общий controller; смена режима активного цикла отклоняется | Новый controller не выбран в production runtime; native state migration отсутствует |
| Fixed SL recovery | Долг по фактическому net, включая обязательные commission/swap/fee открытия и закрытия; N=3, оставшиеся части, новый SL добавляет долг и делит на N; no forgiveness даже 0.01; BUY/SELL зеркала, OCO, фиксированный SL без trailing | Долг/части читаются из controller; демонстрационные примеры | Native CLOSE/evidence и денежный ledger нового модуля не интегрированы с MT5; simulator settlement не выдаётся за native ledger |
| Движки и обычный TP | Один engine на цикл; switch со следующего полного цикла. Legacy stepped engine сохранён. В новом controller ordinary TP default 0.25 | Настройки engine/N/расстояний; одна атомарная запись mode/phase/modules при смене режима | Legacy production Settings default остаётся 0.20; не мигрирован молча |
| N/TP/SL во время позиции | N переразбивает текущий долг заново, немедленно меняет TP; текущий SL не меняется. Достигнутый новый TP -> отдельный CLOSE intent и фактическое подтверждение. Ручные TP/SL сохраняются до явного пересчёта N | Browser Apply N проверен до simulator readback; drag/edit TP/SL old/new -> Apply | Новый protective/close contract реализован только fake-адаптером |
| Manual close | Подтверждение ID/size, фактические deals/costs, долг пересчитан, новые входы остановлены. Полностью погашенный цикл архивируется с reason MANUAL; непогашенный возобновляется только явной командой. UNKNOWN не повторяется | Кнопка -> preview -> Apply -> общий controller -> fake close -> WAIT_TRIGGER проверены браузером | Native market close и его reconciliation не реализованы |
| Pending replace/delete | Замена только после подтверждённых отмен; fill/cancel race сохраняет позицию и долг. UNKNOWN cancellation не повторяется. Crossed levels отказ без MARKET. Чужие/неидентифицированные позиции блокируют sync-команды | Tap/drag existing side показывает перенос с old price; delete требует подтверждения | Новые handlers не подключены к native adapter/runtime |
| Telegram /start и общий controller | /start предлагает auto/semi и URL-кнопку; callback проверяет chat/user, /action использует durable gateway с защитой повторов после crash. UI owner mailbox -> тот же InteractiveController.action. Браузер не владеет loop | URL ведёт только на localhost mock, не открывается автоматически. HTTP transport отдельно от owner/SQLite | Реальный Telegram worker не запускался; реальный URL/доступ/credentials вне этапа. С телефона localhost означает сам телефон, это не готовая удалённая ссылка |
| График | Отдельная вкладка, read-only entry, цветные уровни, tap/drag preview, явный Apply, delete/close confirm, pan/pinch/третье касание не создают preview, zoom. Backend уровни/quotes из fake | XAUUSD.s, явно synthetic M1 свечи и демонстрационные зоны. Два режима: статический макет и подключённый mock controller | MT5 history/candles feed не реализован; свечи и зоны не являются native-данными, зоны пока демонстрационные |
| Раз в секунду, freshness | CLI генерирует только явно synthetic quote раз в секунду; UI опрашивает backend раз в секунду. Timestamp меняется при quote, не от heartbeat. Stale/disconnect блокирует Apply; очередь и owner независимы от HTTP клиента | Статический макет имеет отдельный отмеченный fake поток и кнопки stop/disconnect | Реальный MT5 tick feed, его подписка и доставка браузеру отсутствуют |
| Доступ и сохранность | Только 127.0.0.1; exact Host/Origin, no cross-site requests, bounded JSON/queue, CSP, без frontend secrets. Durable command receipts, unknown/502 сверяются, не replay. Рестарт controller проверен с сохранёнными broker facts | Mock CLI намеренно создаёт новую временную БД при каждом запуске; reload страницы backend не сбрасывает | Production authentication/TLS/network configuration, credentials, постоянный market store и restart broker feed не реализованы и не настроены |

## Проверка

- Полный `tools/run_offline_tests.py`: **466 tests OK**, `Blocked real network attempts: 0`.
- Chromium `tools/test_local_panel.py`: **18 tests OK**. Включает UI -> transport queue ->
  controller -> fake broker: TP Apply, N Apply, manual close, потерянный ответ, HTTP502.
  Запросы перехватываются и исполняются из файлов/чистого handler; HTTP сервер не запускался.
- Независимый reviewer повторил все 466 backend tests, а также 17 UI tests до добавления
  последней регрессии N Apply. Последний N Apply проверен основным агентом в полном UI run.
- Проверены рестарт, неизвестный исход, double-send prevention, fill/cancel race,
  новый SL, 3 полных TP, остаток 0.01, costs, SELL mirror, time latch и работа защиты.
- `git diff --check` без ошибок. Реальные MT5/Telegram/торговые сервисы не запускались.
  Системные часы не менялись; push/PR/deploy/publication не выполнялись.

## Локальное воспроизведение

Из корня исходников, Python 3.10+ с requirements проекта:

```sh
python tools/run_offline_tests.py
python tools/test_local_panel.py
python -m tradfi.local_panel
```

UI tests требуют Playwright и установленный Chromium `/usr/bin/chromium` (не входят
в архив как бинарники). Последняя команда — опциональный локальный mock preview:
`http://127.0.0.1:8765/?controller=mock`. Она не была запущена при проверках.
`/` без query — самостоятельный UI mock. Оба режима запрещены для публичного размещения.
Mock CLI не импортирует MetaTrader5 и не содержит токена Telegram. Порт жёстко loopback.

## Границы и следующие шаги

Внешних отказов или блокера доступа к исходникам нет. Оставшаяся работа — техническая
production-интеграция: native CLOSE intent/evidence, atomic cost-aware ledger и migration,
подключение нового controller в runtime, native quote/candle adapter и реальная access
модель. Это не готово в данном commit. Снимать backend restriction ради демонстрации нельзя.
Все это можно проектировать и проверять fake-API отдельно; реальное подключение и торговля
не разрешены текущим заданием. Неоднозначная семантика начала ветки позднего SL не менялась:
как в baseline, clock ветки запускается в момент наблюдения, а не из непроверенного MT5 time.
