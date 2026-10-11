# Отдельная стратегия TradFi / MT5

Локальная ветка реализации: `tradfi-mt5-recovery`, база `work@459c166`.
`main.py`, `trader/` и Capital.com S1–S9 не меняются. Новый запуск — `tradfi_main.py`.
Этот документ не подтверждает готовность к реальной торговле: исходник EA подготовлен, но
MetaEditor/Windows/MT5 и счёт Bybit в среде разработки отсутствуют. EA здесь не компилировался,
торговых запросов и реального Telegram не было. Demo XAUUSD.s не предполагается.

## Устройство

| Файл | Назначение |
| --- | --- |
| `tradfi/settings.py` | Полный атомарный блок настроек, без частичного применения |
| `tradfi/broker.py` | Живая спецификация, денежный TP через калькулятор брокера, ступени |
| `tradfi/engine.py` | Один владелец состояния, логический Recovery-цикл и OCO |
| `tradfi/storage.py` | Отдельная SQLite WAL/FULL, intent до отправки, журнал переходов |
| `tradfi/simulator.py` | Искусственный брокер для offline-тестов |
| `tradfi/gateway.py` | Локальные Common Files: команды, ответы и снимок MT5 |
| `mt5/TradFiGateway.mq5` | EA-исполнитель: никакой стратегии или вычисления Recovery |
| `tradfi/telegram.py` | Только `/settings`, `/start`, `/stop`, `/status`; разрешённый chat |
| `tradfi/runtime.py` | Отдельный запуск, OS-lock SQLite и mailbox, логи |
| `tradfi/diagnostics.py` | Offline-выгрузка состояния и последних 500 записей журнала |

Поток: Telegram → Python/SQLite → локальный mailbox → EA → MT5. Ответы и broker facts идут
обратно. Публичный HTTP-сервер, вебхуки, DLL и удалённый терминальный доступ не нужны.
Один Python-процесс на mailbox и SQLite, один EA на mailbox. EA удерживает эксклюзивный файл;
Python использует Windows `msvcrt.locking` (в Linux тестируется `flock`). Не копировать активную
SQLite WAL-базу как единственный файл: использовать SQLite backup API или остановить владельца.

## Настройки и команды

`/settings` возвращает цельный блок кода для копирования. Весь блок длиннее лимита 256 символов
[CopyTextButton](https://core.telegram.org/bots/api#copytextbutton), поэтому не разбит на несколько
кнопок: скопировать текст, изменить значения и отправить **одним сообщением**.

```text
/SETTINGS:
INITIAL_SIZE=0.09
BRANCH1_RANGE=3.00
BRANCH1_SL_DISTANCE=2.00
BASE_TP_DISTANCE=0.20
STEPPED_TP_ACTIVATION=0.70
STEPPED_TP_STEP=0.10
RANGE_AFTER_PARTIAL_TP=2.00
SL_DISTANCE_AFTER_PARTIAL_TP=1.50
RANGE_AFTER_SL=2.00
SL_DISTANCE_AFTER_SL=1.50
CYCLE_COOLDOWN_SECONDS=60
```

Это параметры стратегии, **не спецификация инструмента**. Размер — в лотах; дистанции — в цене.
Неизвестные/повторные/пропущенные ключи, NaN/Infinity, неположительные значения и параметры вне
реальных volume/tick/stops ограничений отклоняют весь блок. Старые настройки остаются.
Активный цикл хранит собственный snapshot: изменения применятся только в следующем цикле.

`/start` разрешает вход и запоминает последнее уже закрытое M1 bar ID. Ждём строго новый закрытый
бар, используем его Close. Повтор `/start` не пропускает cooldown. `/stop` при открытой позиции
разрешает завершить **весь логический Recovery-цикл**, но не начать следующий. При одних pending
заявках они отменяются с подтверждением; snapshot незавершённого цикла сохраняется в
`PAUSED_CYCLE`. `/start` после доказанного flat возобновляет ту же ветку. Между ветками `/stop`
также сохраняет Recovery и ждёт `/start`. Fill, обнаруженный при отмене, переключает остановку
на завершение текущего цикла. Два исполнения OCO дают сохраняемый `SAFE_PAUSE` и уведомление;
`/start` не стирает это состояние. Автоматических закрытий сторон нет.

Telegram получает сообщения в отдельном worker; торговый владелец атомарно сохраняет команду и
update offset в SQLite. При первой инициализации базы старый Telegram backlog отбрасывается.
Повтор update ID не повторяет торговлю. Уведомления имеют durable outbox; потерянный sendMessage
ack может дать повтор уведомления, но не повтор торговой команды. Не использовать один token
одновременно с Capital-ботом: `getUpdates` и меню бота должны иметь одного владельца.

## Стратегия и деньги

1. Первая пара: BUY STOP `CENTER + BRANCH1_RANGE`, SELL STOP `CENTER - BRANCH1_RANGE`.
   В обоих запросах уже есть настоящие SL и TP. После одного fill отменяется противоположный.
   Исходные trigger и actual entry хранятся раздельно. После fill SL пересчитывается от actual
   entry с той же дистанцией, TP — под денежную цель, старая защита не снимается до модификации.
   Первичное reanchor может сдвинуть SL в любую сторону для сохранения заданной дистанции;
   **последующие ступени** только улучшают SL.
2. Денежная цель первой позиции = `OrderCalcProfit(side, actual lot, actual entry,
   actual entry ± BASE_TP_DISTANCE)`. Recovery = цель − сумма фактического `DEAL_PROFIT` закрытий.
   Комиссии и swap исключены. Spread/slippage не добавляются отдельно: они уже отражены в fills/P&L.
3. Первый SL возвращает оригинальные два trigger и SL Ветки 1, но TP теперь покрывает Recovery.
   Это единственное повторение исходной пары по правилу первого SL.
4. У Recovery-входов денежный TP рассчитывается через `OrderCalcProfit` для фактического размера.
   Производный TP выбирается по целым тикам наружу до покрытия суммы; это явно заданное правило
   денежного TP, не скрытое округление пользовательских уровней/расстояний.
5. Ступени разрешены только Recovery-позиции при TP-distance ≥ activation. При равенстве цены
   TP сначала ждём broker TP, отдельного close нет. Для BUY движение считается по BID, SELL по ASK.
   На +0.70 SL становится entry, на +0.80 — entry+0.10 (BUY), далее аналогично; SELL зеркален.
   Confirm/read-back модификации либо нативные DEAL_SL/DEAL_TP нужны для признания новой защиты.
6. Полное закрытие по подтянутому SL → CENTER=actual close, ветка `PARTIAL_TP`; это имя маршрута,
   **не частичное закрытие объёма**. Даже нулевой/отрицательный P&L остаётся на этом маршруте.
   Обычный SL Recovery-позиции → CENTER=actual close, ветка `AFTER_SL`.
7. После TP или подтянутого SL остаток ≤ $1 завершает цикл. Допуск записывается отдельно как
   `forgiven_residual`, не прибыль. К обычному SL этот допуск не применяется. TP с недопокрытием
   больше $1 продолжает Recovery по ветке `PARTIAL_TP` от actual close.
8. Завершение требует разрешённых OCO-команд, отсутствия ордеров и позиций. Затем cooldown из
   snapshot (по умолчанию 60 секунд), затем новое закрытие M1 — новый CENTER и новая цель.

Нет partial-close, refill, SL Stage 1–4 или bot-close на откат. Частично исполненный/изменённый
объём, внешнее неоднозначное закрытие, неизвестная позиция и конфликт identity дают SAFE_PAUSE.
Если actual weighted entry не допускает точную защиту по tick size, старый SL/TP сохраняется;
недопустимая модификация не отправляется. Stop/freeze ограничения проверяются заново перед I/O.

## Broker-layer и надёжность

EA отдаёт contract size, tick size, tick value profit/loss, volume min/step/max, point,
stop/freeze levels, валюты, calc mode, quotes, M1 Close, orders, positions, order history и deals.
Числа сериализуются с 8 десятичными знаками; символы с digits >8 отклоняются. Нет зашитых
100 единиц контракта или 0.01 лота. USD account обязателен для точного смысла «$1»; hedging mode
обязателен для обнаружения двух исполнений OCO. Netting и другие валюты блокируются.
`OrderCalcProfit` возвращает деньги в валюте счёта; формула по условному contract size не
подменяет этот вызов. В simulator используются **явные искусственные**, не Bybit значения.

Протокол v1: `snapshot.json`; `commands/<24hex>.cmd` (UTF-8 `key=value`); `acks/<id>.json`;
`ledger/<id>.intent`. Команда содержит account identity и срок годности. Запись — temp/fsync/rename
в Python, FileFlush/FileMove в EA. Перед каждой мутацией Python фиксирует may-send в SQLite,
EA фиксирует marker перед OrderSend. Один и тот же id EA не отправляет повторно. Ответ
OrderSend=true сам по себе **не fill**: нужны retcode и снимки orders/deals.

MARKET разрешён, когда STOP уже пройден по свежей котировке; исходный trigger сохраняется.
Если цена прошла между Python и EA, EA может явно ответить `REJECTED/CROSSED` **до OrderSend**;
только этот доказанный отказ разрешает новый защищённый MARKET. UNKNOWN не разрешает fallback.
Для MARKET EA требует FOK, иначе отказывает; брокерскую поддержку FOK нужно проверить отдельно.

При restart SQLite + broker comment/order ID + position identifier + deal ID восстанавливают
владельца. Отсутствие ордера не равно отмене. UNKNOWN MODIFY может разрешиться по SL/TP read-back
или нативным полям закрывающего deal; UNKNOWN DELETE — по terminal order history. При потере
команды до публикации, но после may-send, операция может навсегда остаться UNKNOWN: это сознательный
запрет слепого повторения. Для снятия SAFE_PAUSE/неидентифицируемого UNKNOWN нужен отдельный
проверяемый процесс брокерской сверки; команды, стирающей состояние вслепую, нет.

Полное подтверждённое закрытие позиции делает оставшийся UNKNOWN MODIFY недействующим:
он становится `OBSOLETE`, **не DONE**. Это позволяет учесть фактический P&L, не утверждая, что
новый SL установился. Нативный DEAL_SL закрытия отличает обычный SL от подтянутого. READY MODIFY,
не пересёкший may-send, при закрытии не отправляется. Отмена OCO отправляется до запроса
калькулятора прибыли: timeout OrderCalcProfit её не задерживает.

Явный REJECTED отмены допускает новый durable intent только при повторном подтверждении живого
ордера. Максимум три попытки, затем SAFE_PAUSE с уведомлением; UNKNOWN не повторяется.
Повтор `/stop` между ветками сохраняет уже принятое намерение FINISH логического цикла.

EA запрашивает всю доступную history, а не скользящее короткое окно. Broker-side усечение истории
невозможно доказательно исключить одним HistorySelect: при недостатке identity обработка ждёт,
не выдумывает результат. Ledger/acks и SQLite не удалять при незавершённых циклах. Snapshot и
журнал команд растут с историей; политика долгосрочного архивирования требует отдельной проверки.

## Windows/VPS: подготовка без торговли

1. Выделить одну папку проекта и один локальный mailbox на том же Windows VPS, что MT5.
   Common Files обычно находится в `%APPDATA%\MetaQuotes\Terminal\Common\Files`;
   фактический путь сверить с `TERMINAL_COMMONDATA_PATH` конкретной установки.
2. Создать Python 3.12 venv, установить `requirements.txt`. Скопировать
   `tradfi_config.example.json` в `tradfi_config.json`, исправить локальные пути.
   Оставить `enable_trading=false`. Не копировать Capital state/config.
3. Открыть `mt5/TradFiGateway.mq5` в MetaEditor, скомпилировать и сохранить полный build log.
   Требуются отсутствие ошибок и ручная проверка предупреждений. Здесь этот шаг не выполнен.
4. До любого подключения к реальному счёту проверить EA в изолированном MT5 Strategy Tester с
   искусственным символом/котировками или подходящим тестовым окружением. Реальная торговля
   XAUUSD.s не заменяет отсутствующее demo. Проверить файловые флаги, таймер, JSON, история/identity,
   retcode, исполнение защищённых pending/MARKET, FOK, OCO race и restart в каждой may-send границе.
5. Для будущего read-only осмотра терминала EA должен иметь `AllowTrading=false`, без включения
   автоторговли. Запустить только `python tradfi_main.py --mode gateway --inspect --config tradfi_config.json`.
   Команда читает snapshot, не запускает Telegram и не отправляет мутации. В этой работе она не
   выполнялась против MT5. Удалять account/login из выгрузки перед передачей другим.
6. Значения `TRADFI_TELEGRAM_TOKEN`, `TRADFI_TELEGRAM_CHAT_ID` предусмотрены как переменные окружения
   **текущего процесса**, не постоянные настройки Windows. Секреты здесь не задавались.
   Рабочий gateway-loop требует обоих явных торговых флагов Python/EA и разрешений MT5; в рамках
   текущей задачи их не включать. Не добавлять автозапуск или планировщик до отдельной проверки.

Логи: `tradfi.log` (ротация 5 × 5 MB), SQLite audit. Offline диагностика:
`python -m tradfi.diagnostics tradfi_state.sqlite3 tradfi_diagnostic.json`.
Экспорт скрывает account identity, но торговая история остаётся приватной.

## Проверка в Linux без сети

```bash
python tradfi_main.py --mode simulate
python -m unittest discover -s tests -p 'test_tradfi*.py' -v
python -m unittest discover -s tests -v
```

Симулятор не вызывает MT5/Telegram и не подключается к брокеру. Тесты проверяют деньги, число
мутаций, SL/TP, смену веток, зеркальный SELL, settings snapshot, $1 boundary, cooldown/M1,
duplicate events/updates, UNKNOWN, commit failure и restart, OCO и stop/cancel/fill гонки.
Проверка Python и local-mailbox тесты не являются доказательством компиляции EA или совместимости
с реальным Bybit TradFi. До этого остаются отдельные Windows/MetaEditor/MT5 приёмочные проверки.

Фактический прогон 2026-10-11: Linux, Python 3.12.14; 52 новые TradFi-регрессии и полный набор
490 тестов — OK (44.618 секунды для полного набора). Сокетные соединения и HTTP были заблокированы
через тестовый `sitecustomize.py`. Offline-пример завершил цикл с искусственной целью/P&L $3.60
и тремя командами (два защищённых STOP и отмена противоположного). Реальные спецификация,
Telegram, MT5, Bybit и MQL5 compiler этим прогоном не проверялись.

## Независимый обзор после 99c52a4

Повторная проверка 2026-10-11: 61 TradFi-тест (независимый ревьюер повторил весь набор) и
499 тестов проекта — OK, 44.278 секунды, сеть заблокирована. База origin/work после fetch:
`459c1661cec2060193027d0039e830cf83a94e16`. Публикации не было.

Исправлены: повторный stop терял FINISH между ветками; OCO-отмена ждала калькулятор;
READY MODIFY мог отправляться уже закрытой позиции; UNKNOWN MODIFY блокировал учёт закрытия;
REJECTED CANCEL оставлял pending без обработки; ASCII mailbox не принимал Unicode server name;
одновременные deal tickets сортировались как строки вместо чисел.

| Требование | Проверка |
| --- | --- |
| Native SL/TP у pending/MARKET, actual-fill reanchor | Симулятор + статический путь MqlTradeRequest/OrderCheck/OrderSend |
| Денежный target/Recovery, без fees/swap | Явная искусственная спецификация и broker profit; EA вызывает OrderCalcProfit |
| Первый SL / повтор пары / последующий SL | Исходные уровни один раз; затем actual close и новые дистанции |
| Ступени BUY/SELL и TP priority | Исполнимая сторона, точные activation/step границы и TP equality |
| Подтянутый SL с +/0/−; допуск $1 | Native SL подтверждение, правильный маршрут, $1/$1.01, запрет допуска для обычного loss SL |
| OCO race, crossed STOP | Два fills → SAFE_PAUSE; MARKET только crossed или explicit pre-send CROSSED reject |
| UNKNOWN, crash/restart | Intent до I/O, отсутствие повторов, read-back, полное закрытие → OBSOLETE MODIFY |
| Cancel reject / stop / branches | Только explicit reject retries, предел 3, stop FINISH устойчив к повтору/restart |
| Settings и следующий цикл | Атомарность, immutable snapshot, cooldown и строго новое M1 close |
| Transport / Telegram | Local mailbox UTF-8, identity/expiry/locks, allowed chat и durable update offset |

EA — реальный исходник исполнителя, не stub; это результат чтения кода, **не MQL5 build/MT5 test**.
Идемпотентность — предотвращение повторной отправки одного command ID с консервативным UNKNOWN,
а не гарантия exactly-once исполнения у брокера. Потеря файла после SQLite may-send или потеря
достаточной broker identity может требовать ручной сверки. Эти ограничения сохраняются.

Источники протокола:
[OrderCalcProfit](https://www.mql5.com/en/docs/trading/ordercalcprofit),
[OrderSend и подтверждения](https://www.mql5.com/en/docs/trading/ordersend),
[Symbol properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants),
[Deal properties](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties),
[FileMove](https://www.mql5.com/en/docs/files/filemove).
