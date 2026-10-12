# TradFi: прямое подключение к MetaTrader 5

Текущий транспорт — официальный Python-пакет `MetaTrader5`, backend `mt5_direct_v1`.
EA, `FILE_COMMON`, snapshot/command/ack-файлы больше не участвуют в новом исполнении.
Стратегия, Recovery в UST, SQLite durability и Telegram-команды остаются прежними.
Legacy Capital.com (`main.py`, `trader/`, его зависимости) не меняется.

## Минимальная подготовка Windows VPS: только чтение

Новый проект распаковать в отдельную папку. Открыть её в командной строке и выполнить:

```bat
py -m venv .venv
.venv\Scripts\python -m pip install -r requirements-tradfi-mt5.txt
copy tradfi_config.example.json tradfi_config.json
.venv\Scripts\python tradfi_main.py --mode mt5 --inspect
```

Перед последней командой проверить `terminal_path` в конфиге. Значение по умолчанию:
`C:/Program Files/MetaTrader 5/terminal64.exe`. Терминал должен быть уже открыт и авторизован.
Пароль/логин для нового входа адаптер не запрашивает: используется существующая сессия.
`expected_server=Bybit-Live-6`, `symbol=XAUUSD.s`, счёт hedging и валюта UST проверяются.
`expected_login=null` разрешён только для read-only проверки: подключение привязывается к
текущему счёту на время процесса, смена счёта не принимается молча.

Оставить **Algo Trading OFF**, `enable_trading=false`, `legacy_ea_removed=false`, `legacy_state_reconciled=false`.
`--inspect` принудительно запрещает торговлю даже если конфиг ранее разрешал её. Команда
не создаёт SQLite, не импортирует Telegram, не требует token/chat ID. Она читает терминал,
спецификацию, свежую котировку и вызывает только калькулятор гипотетического P&L.
Вывод исключает account/login; ошибки выводятся без исходного приватного текста терминала.
Успешный preflight не является проверкой торгового исполнения. Устаревшая котировка
(предел 5 секунд), закрытый рынок, неподходящая валюта/hedging или недоступная история
не обходятся увеличением таймаутов или подменой времени.

Python 3.13.16 x64, MetaTrader5 5.0.6231 и terminal build 6254 уже проверены пользователем
только для прямого read-only подключения и `order_calc_profit`. Это не приёмка нового
адаптера целиком. На Linux импорт MT5 отложен; регрессии используют fake module и не
выполняют реальные торговые операции. Пакет MT5 устанавливается только на Windows
через platform marker; старый `requirements.txt` сохранён.

## Переход со старого EA и владение состоянием

Старый оставшийся `PROFIT.cmd`, EA и mailbox автоматически не изменяются и не исполняются
новым адаптером. Перед будущим запуском исполнительного режима оператор должен **снять
старый EA со всех графиков**, проверить отсутствие второго исполнителя и только затем
явно подтвердить `legacy_ea_removed=true`. Сейчас торговые разрешения не включать.

Новая база по умолчанию — `tradfi_mt5_state.sqlite3`. Базу старого файлового backend,
её WAL/SHM, journal, ledger и ack сохранить. Нельзя копировать старое состояние в новую
базу, переименовывать backend, удалять pending intents или сбрасывать Recovery для обхода
проверки. Сохранённое состояние без совместимой метки backend блокируется до MT5 I/O;
автоматической миграции даже формально пустого старого snapshot нет. Незавершённый старый
цикл требует отдельной сверки, а не старта новой базы поверх существующих обязательств.
Отдельное подтверждение `legacy_state_reconciled=true` обязательно перед исполнением даже
с новой пустой базой. Оператор должен проверить, что прежняя стратегия действительно не
запускалась и не оставила денежных обязательств/состояния, либо завершить сверку старой базы
с брокером. Снятие EA само по себе этого не подтверждает. Удаление/перемаркировка базы не
считается сверкой. Read-only `--inspect` этого подтверждения не требует.

Для будущего исполнения обязательны одновременно: `enable_trading=true`,
`legacy_ea_removed=true`, `legacy_state_reconciled=true`, точный положительный
`expected_login` и разрешения терминала/счёта.
Адаптер сам разрешения не меняет. Отдельный `magic=25101102` не совпадает с прежним EA.
Чужие ручные позиции не становятся собственными из-за совпадения символа.

Процесс получает OS-lock базы и общий lock счёта/server/symbol в
`%LOCALAPPDATA%\AI IMPULS TradFi\locks` **до подключения MT5**. Поэтому разные папки проекта
или имена базы одного Windows-пользователя не создают двух владельцев. Два разных Windows
пользователя или VPS этим локальным lock не координируются: такой запуск запрещён операционно.
Read-only inspect не запускает владельца стратегии. Telegram использует прежние переменные
процесса `TRADFI_TELEGRAM_TOKEN` / `TRADFI_TELEGRAM_CHAT_ID`; они не нужны сейчас.

Режим `--mode gateway` остановлен с сообщением о переходе; старый `FileGateway` и EA
оставлены как исторические исходники. Обычный запуск без `--mode` по-прежнему выполняет
только offline simulation. Ротационный лог прямого исполнения: `tradfi_mt5.log`.
Offline export новой базы: `python -m tradfi.diagnostics tradfi_mt5_state.sqlite3 report.json`;
экспорт содержит приватную торговую историю, хотя account скрыт.

## Деньги и ограничения исполнения

Цели и допуск завершения **1 UST** опираются на `order_calc_profit` в валюте счёта;
Recovery — на фактический broker deal profit без комиссии/swap. Реальное поле
`profit_currency=USD` не переименовывается; конвертации UST/USD/USDT нет. Правила TP,
подтянутого SL и обычного SL остаются описанными ниже.

Intent фиксируется до `order_send`. Потерянный/неопределённый ответ означает UNKNOWN,
не разрешение повторить заявку. Reconciliation использует ticket/position identity,
orders/history orders/deals, проверяет ownership и не суммирует повторный deal второй раз.
Это не exactly-once гарантия MT5. Недостаточная история или неоднозначность требует
остановки/сверки, а не предположения, что заявки не было. OCO остаётся неатомарным:
двойной fill переводит стратегию в SAFE_PAUSE. SL/TP адресуются конкретному hedge ticket.
Нормализация объёма/цены, stops/freeze, filling/expiration modes и свежесть проверяются
по терминалу; терминальные отказы сохраняются, торговые защитные проверки не обходятся.

### Журнал прямого адаптера и пределы проверки

`tradfi/mt5_adapter.py` использует ту же SQLite connection (WAL/FULL), отдельные таблицы
`mt5_binding`, `mt5_commands`, `mt5_events`, `mt5_facts`. Сначала контроллер сохраняет UNKNOWN,
затем адаптер сохраняет неизменяемый исходный intent, результат `order_check`, запрос и may-send,
и лишь затем вызывает `order_send` один раз. Raw result, retcode и last_error сохраняются локально.
Сбой записи до send запрещает отправку; сбой после send оставляет durable UNKNOWN. Повторный
command ID не отправляется, изменение payload под тем же ID отклоняется. Противоречие между
snapshot контроллера и журналом адаптера блокирует восстановление до подключения терминала.
UNKNOWN entry блокирует последующие новые входы до положительного broker evidence;
отмена уже подтверждённого pending при этом остаётся допустимой.

Orders и позиции считаются собственными только при связи с локальным отправленным intent,
правильным magic/symbol/side/объёмом и точным comment или возвращённым order ticket.
Один magic или похожая цена не устанавливают ownership. В snapshot остаются чужие позиции и
ордера: контроллер видит конфликт и останавливается. POSITION_IDENTIFIER хранится как id,
для `TRADE_ACTION_SLTP` заново выбирается текущий POSITION_TICKET. Команд закрытия позиции
в интерфейсе адаптера нет. MODIFY/CANCEL требуют broker readback, одного ответа DONE недостаточно.

Python TradeDeal не содержит нативных полей DEAL_SL/DEAL_TP. Источник защитных уровней —
только точно сопоставленный закрывающий history order, если он есть; иначе передаются нули.
Запрошенные уровни не выдаются за подтверждённые уровни закрытия. UNKNOWN-модификация без
такого подтверждения не становится успешной из-за исчезновения позиции.

История запрашивается целиком и дополнительно сохраняется по broker ticket с обнаружением
противоречий. `None` от API не подменяется пустым списком, отсутствие данных не доказывает
отсутствие исполнения. Частичные входы/выходы и недостающая история могут остановить цикл
в SAFE_PAUSE: автоматического долива/дозакрытия нет. Счёт с большой историей может давать
медленный snapshot; при нарушении freshness отправка блокируется. Оптимизация окна истории
сейчас не вводилась. Несколько API-чтений и смена счёта относительно `order_send` не атомарны;
проверки до/после и UNKNOWN ограничивают риск, но не создают exactly-once гарантию.

STOP использует RETURN и требует постоянного GTC вместе с сохраняемыми SL/TP. MARKET
использует FOK по разрешениям execution/filling mode. Значения объёма и цен проверяются
на точную сетку, не округляются молча; для market execution поле request.price опускается
по контракту MT5, встроенные SL/TP всё равно проверяются. Order check не гарантирует send;
ограничения и выбранный ticket повторно сверяются после долговечной записи intent.

Проверки локальной реализации: **559 tests, OK (47.742 s)**, включая 44 fake-MT5
регрессии. Полный набор выполнялся с блокировкой сети; реальные MT5/Telegram не вызывались.
Проверены crash до/после send, UNKNOWN/restart/duplicate, history lag/overlap, actual fills,
partial и dual fill, ticket-aware SLTP, frozen protection, смена quote/spec после intent,
backend/account/currency compatibility и read-only CLI. Независимое ревью выполнено;
исправлены повторная проверка свежести после history I/O и явное подтверждение сверки старой базы.
Нативный Windows preflight нового адаптера и live order_send не выполнялись.

Официальный API: [initialize](https://www.mql5.com/en/docs/python_metatrader5/mt5initialize_py),
[order_calc_profit](https://www.mql5.com/en/docs/python_metatrader5/mt5ordercalcprofit_py),
[order_send](https://www.mql5.com/en/docs/python_metatrader5/mt5ordersend_py).

---

## Архив: первоначальный файловый gateway и этапы EA 1.00–1.02

**Ниже сохранена история проекта и описание стратегии. Старые команды установки EA и
`--mode gateway` не являются текущим порядком запуска.** Прямой MT5 workflow приведён выше.

### Историческая реализация TradFi / MT5

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
7. После TP или подтянутого SL остаток ≤ 1 UST завершает цикл. Допуск записывается отдельно как
   `forgiven_residual`, не прибыль. К обычному SL этот допуск не применяется. TP с недопокрытием
   больше 1 UST продолжает Recovery по ветке `PARTIAL_TP` от actual close.
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
100 единиц контракта или 0.01 лота. UST account обязателен для порога 1 UST; hedging mode
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
мутаций, SL/TP, смену веток, зеркальный SELL, settings snapshot, 1 UST boundary, cooldown/M1,
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
| Подтянутый SL с +/0/−; допуск 1 UST | Native SL подтверждение, правильный маршрут, 1 UST/1.01 UST, запрет допуска для обычного loss SL |
| OCO race, crossed STOP | Два fills → SAFE_PAUSE; MARKET только crossed или explicit pre-send CROSSED reject |
| UNKNOWN, crash/restart | Intent до I/O, отсутствие повторов, read-back, полное закрытие → OBSOLETE MODIFY |
| Cancel reject / stop / branches | Только explicit reject retries, предел 3, stop FINISH устойчив к повтору/restart |
| Settings и следующий цикл | Атомарность, immutable snapshot, cooldown и строго новое M1 close |
| Transport / Telegram | Local mailbox UTF-8, identity/expiry/locks, allowed chat и durable update offset |

EA — реальный исходник исполнителя, не stub; это результат чтения кода, **не MQL5 build/MT5 test**.
Идемпотентность — предотвращение повторной отправки одного command ID с консервативным UNKNOWN,
а не гарантия exactly-once исполнения у брокера. Потеря файла после SQLite may-send или потеря
достаточной broker identity может требовать ручной сверки. Эти ограничения сохраняются.

## Исправление JSON EA 1.01

По сообщению пользователя, исходная версия собралась в MetaEditor x64 build 6254 на Windows
Server 2022 без ошибок и предупреждений, однако `json.tool` отклонил snapshot на char 118.
Причина: `(string)datetime` выводит календарную дату, а исходные `now` и `candle.id` вставляли
её без JSON-кавычек. Теперь все три поля времени snapshot используют `IntegerToString((long)...)`.
`deals[].time` уже является целым DEAL_TIME_MSC и сохраняет миллисекунды.
Общий Q-helper также экранирует весь диапазон U+0000–U+001F, включая TAB/BS/FF в broker comments
и ack reason; кавычки, обратная косая черта и Unicode сохраняются.

Документированная семантика:
[MQL5 Date and Time: приведение datetime к string и long](https://www.mql5.com/en/book/common/conversions/conversions_datetime),
[IntegerToString](https://www.mql5.com/en/docs/convert/integertostring).

`tests/test_tradfi_ea_json.py` извлекает реальные функции сериализации из `.mq5` и исполняет их
через C++ adapter `tests/fixtures/mt5_json_shim.hpp` с искусственными MT5 данными. Восстановление
старого datetime cast воспроизводит char 118 / column 119 на вымышленном девятизначном account;
отдельно проверяется дефект candle.id после исправления только now. Покрыты пустой/заполненный
snapshot, оба boolean, 64-bit timestamps и tickets, Unicode/control characters, trade/profit/error
и UNKNOWN ack. Это **не** запуск EA в MT5 и **не** проверка его MQL5-компиляции. Если g++ отсутствует,
семь исполняемых adapter-тестов пропускаются, source guard остаётся; здесь g++ был доступен.
Локальная проверка: 8 новых JSON-тестов прошли; полный offline-набор — 507 тестов, OK.

Для исправленной версии 1.01 нужен новый build в MetaEditor. Стратегия и торговые команды этим
исправлением не меняются. Пока остаются `AllowTrading=false` и Algo Trading OFF. После обновления
и пересборки в папке mailbox можно повторить безопасную проверку, не выводящую account/deals:

```bat
py -m json.tool snapshot.json >NUL && echo JSON_OK
```

Реальный snapshot для диагностики не запрашивался, подключения к VPS/счёту из среды разработки не было.

## Денежная модель UST / EA 1.02

Пользователь подтвердил расчёт всех денежных целей, фактического P&L и Recovery в **UST**.
Порог завершения — **1 UST**, только после broker TP или подтверждённого подтянутого SL;
обычный SL не получает этот допуск. `forgiven_residual` не прибавляется к фактической прибыли.
`OrderCalcProfit` возвращает расчёт в валюте счёта, а Recovery уменьшается на фактический
`DEAL_PROFIT` закрытий. Комиссия и swap исключены. Пересчёта валют нет.

В переданной пользователем спецификации `currency=UST`, `profit_currency=USD`, `calc_mode=2`.
Это разные поля: Python и EA сохраняют оба фактических значения, не переименовывая валюту
прибыли инструмента. Размер контракта 100, tick size 0.01, tick values 1, volume min/step 0.01,
volume max 100, point 0.01, stops level 20, freeze level 0 проверяются отдельной offline-фикстурой;
в production эти числа по-прежнему поступают из MT5. Python и EA требуют UST account,
прочие проверки торговли, hedging, identity и freshness не ослаблены.

SQLite сохраняет `currency=UST` в корне состояния, активном цикле, завершённых циклах и новых
строках audit. При восстановлении проверяется валюта состояния и каждого сохранённого цикла.
USD, иная валюта или отсутствие маркировки приводят к `DurabilityError` **до обращения к брокеру**.
Старая база автоматически не пересчитывается, не перемаркируется и не сбрасывается, даже если
в ней нет активного цикла. Её следует сохранить и отдельно сверить; удалять базу для обхода
проверки нельзя. Новая установка без ранее сохранённого состояния создаёт маркированное UST-состояние.
Смена валюты живого snapshot на несовместимую переводит работающий контроллер в SAFE_PAUSE
до сверки сделок/отправки команд, сохраняя накопленный Recovery.

По сообщению пользователя, **EA 1.01** прошёл native build с 0 errors/0 warnings, JSON_OK,
connected=true и hedging=true при выключенной торговле. Это не подтверждает native build **1.02**.
После отдельного разрешения публикации на VPS потребуется обновить Python-пакет `tradfi/`
(старый ZIP блокирует UST) и исходник EA, снять старый EA, пересобрать/заменить `.ex5` версии 1.02
и прикрепить заново. `AllowTrading=false`, Algo Trading OFF и `enable_trading=false` сохраняются.
Проверить JSON и read-only snapshot; при закрытом рынке stale quote закономерно блокирует
штатный PROFIT roundtrip. Telegram credentials для этих проверок не нужны.

Локальная проверка этой версии: 515 тестов, OK (45.966 s), включая 8 новых UST-регрессий
и 8 JSON-тестов. Сетевые обращения были заблокированы offline guard. `git diff --check` пройден.
Native build EA 1.02 и проверка на VPS ещё не выполнялись.

Источники протокола:
[OrderCalcProfit](https://www.mql5.com/en/docs/trading/ordercalcprofit),
[OrderSend и подтверждения](https://www.mql5.com/en/docs/trading/ordersend),
[Symbol properties](https://www.mql5.com/en/docs/constants/environment_state/marketinfoconstants),
[Deal properties](https://www.mql5.com/en/docs/constants/tradingconstants/dealproperties),
[FileMove](https://www.mql5.com/en/docs/files/filemove).
