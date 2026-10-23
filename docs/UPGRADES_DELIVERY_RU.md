# Апгрейды: native pipeline, проверенный offline

База защит времени: `9519208e09f680d003ce893e6dc8688870823d63`.
Предыдущий simulator этап: `8a2de9e35a0e2dd0037915d873f34e9f6299ffc5`.
Ветка `upgrades/local-control`. Этот результат обновляет только архив апгрейдов.
Time-safety ZIP не заменяется. Реальные MT5, Telegram, сеть, сделки и VPS не запускались.

## Матрица реализации

| Требование | Реализация в коде | Проверка offline / ограничения |
|---|---|---|
| Auto/semi | Runtime создаёт InteractiveController с native per-obligation broker. Auto сохраняет прежний M1 алгоритм; semi принимает один/два exact trigger или два диапазона от снимка команды (BUY Ask, SELL Bid), ждёт следующую команду после полного цикла | Runtime → adapter → fake MT5: auto и semi. Existing active legacy cycle получает legacy metadata при загрузке до обработки настроек; долг не переинициализируется |
| Fixed SL recovery | Один engine на цикл, переключение со следующего полного цикла. Фактический net с commission/swap/fee открытия и закрытия, N=3 default, оставшиеся части, новый SL добавляет долг и переразбивает, без прощения 0.01. SL фиксирован на сделку; старый stepped TP сохранён | Fake MT5 OCO, SL, новый recovery, N, costs и atomic ledger/restart. Native closure ledger коммитится вместе с состоянием, не только simulator memory |
| Обычный TP | Для новой БД InteractiveController default 0.25; расчётный recovery TP отдельно | Сохранённые настройки старой БД не переписываются молча, включая ранее установленный TP 0.20 |
| Изменение N/TP/SL | N переразбивает текущий долг на N, сразу рассчитывает TP, SL сохраняется. MODIFY проходит native SLTP и readback. Profitably reached target вызывает отдельный CLOSE с повторной проверкой Bid/Ask/target/profit после order_check | Native tests проверяют SLTP, сохранение SL и гонку потери прибыльного target. Browser Apply → очередь → controller → adapter → fake MT5 |
| Manual close | Exact owned position identifier → текущий ticket, full volume, FOK; durable may-send boundary. EXPERT exit признаётся нашим только при точной корреляции CLOSE/order marker/response/entry identity; фактический net пересчитывает долг и останавливает входы | Runtime → adapter → fake MT5 полный close. Lost reply + restart без второго send; UNKNOWN без подтверждения не повторяется; чужой EXPERT exit блокирует учёт |
| Pending create/replace/cancel | Exact levels с crossed_policy=reject; после подтверждённых отмен новая пара. Fill/cancel race оставляет защиту позиции, UNKNOWN не порождает слепой repeat | Native adapter tests: pair create, OCO, replacement/restart, fill race, unknown cancel. Чужие позиции/ордера не получают права управления |
| Time safety | Baseline latch, async HTTP UTC, ownership/evidence и MT5_TIMESTAMP_SEMANTICS_UNVERIFIED сохранены. Time block отменяет pending, запрещает входы и сохраняет защиту позиции. Unverified quote не используется для новых ценовых действий | Полный baseline runner + native latch cancel/restart/protection tests. Семантика late SL branch clock не менялась: момент наблюдения, не непроверенный MT5 timestamp |
| Telegram /start/button | Native runtime использует тот же controller, chooser auto/semi, URL button при включённой localhost панели. /action идёт через durable ActionGateway; callback auth сохраняется | Telegram double в runtime + прежние Telegram auth/callback tests. Реальный worker/credentials не использованы. Localhost URL с телефона указывает на телефон; удалённый доступ отдельно вне объёма |
| Общий web controller | HTTP workers только очередь/копия view. SQLite и MT5 только owner thread. Action receipts durable, revision/scope проверка; scope включает account/symbol/store UUID, preview сохраняет контекст | Fake transport и Chromium проверяют реальный native adapter путь. Explicit password из окружения, отсутствие auth запрещает API; session expiry, origin/Host, scope и ограничение очереди/JSON/threads |
| Native chart feed | copy_rates_from_pos XAUUSD.s M1, 120 OHLC; snapshot Bid/Ask; feed version/instance/sequence, raw time и отдельное verified UTC. Future candles отклоняются, старые явно STALE, missing feed DISCONNECTED | Fake MT5 bindings с явно INJECTED_MT5. UI получает native candles, не synthetic fallback. Static/mock режимы по-прежнему явно MOCK |
| Секундное обновление | Owner capture не чаще 1 секунды, UI poll 1 секунда. Timestamp котировки не продвигается heartbeat; возраст растёт по monotonic, stale/disconnected видны | Fake clock/feed tests. Реальная задержка native terminal API не измерялась; синхронный вызов MT5 выполняет owner, HTTP клиент его не блокирует |
| График/управление | Отдельная вкладка, цветные BUY/SELL/TP/SL/entry, Bid/Ask, zoom/pan/pinch, tap/drag preview и явный Apply, delete/close confirm; entry read-only. Native зоны связаны с текущими pending | 21 Chromium test, включая native auth/Apply/session/scope. Скриншот INJECTED_MT5 в архиве; данные тестовые |
| Emergency /reset_manual | Fixed reset требует положительных flat/obligation proofs; actual net всех закрытий учитывается атомарно, debt сохраняется в durable carry, прежняя аварийная ветка завершается без списания. WAIT_START/stop ждут явной команды; time_block не снимается. После команды carry атомарно переходит в новый fixed цикл | Native regressions: debt>0/0, costs, двойной OCO fill, restart/duplicate, commit fault, auto snapshot crash, position/pending/UNKNOWN/unavailable отказ. Независимый review пройден |

## Проверки и воспроизведение

- `python tools/run_offline_tests.py`: **495 tests OK**, `Blocked real network attempts: 0`.
- `python tools/test_local_panel.py`: **21 Chromium tests OK**, все HTTP routes перехвачены из файлов/handlers, сервер не запускался.
- В данной среде backend Python: `/workspace/.venvs/ai-impuls-traider/bin/python`.
  Для системного Python с Playwright использован уже установленный requests через
  `PYTHONPATH=/workspace/.venvs/ai-impuls-traider/lib/python3.13/site-packages`.
  На обычной машине нужны зависимости проекта, Playwright и Chromium; бинарники в ZIP не входят.
- Независимый read-only review описан в `UPGRADES_REVIEW_RU.md`.

Native web config: `web.enabled` boolean (default false), `host` только `127.0.0.1`,
`port` default 8765. `TRADFI_WEB_PASSWORD` должен быть явно настроен пользователем
(не менее 32 символов); без него API возвращает AUTH_NOT_CONFIGURED. В frontend
нет credentials; HttpOnly SameSite session. При включении URL —
`http://127.0.0.1:8765/?controller=native`. Это описание реализации, не команда
подключаться к терминалу или торговать. Настройка удалённого доступа/TLS/секретов,
реальные MT5/VPS проверки и deployment не выполнялись и не входят в этот результат.

В ZIP: исходники, patch от time-safety базы, git bundle с дельтой, этот README,
review, оба testlog и явно тестовые screenshots. Никаких новых реальных credentials.

## Завершение fixed reset

Подтверждённое правило реализовано: никакого списания debt, automatic entry или снятия
независимых защит. Если остаток положителен, предыдущий mode и fixed engine сохраняются
до погашения; новая явная команда создаёт новую ветку с сохранённым recovery. При нулевом
остатке carry отсутствует, но reset по-прежнему не считается прибыльным TP-завершением.
Архивный diagnostic log удалён из tracked tree (локальная копия сохранена вне репозитория).
Проверка известных форматов private keys/API tokens не нашла кандидатов в исходниках.
Публикация разрешена только обычным push кода в GitHub origin/work, не запуск сервисов.
