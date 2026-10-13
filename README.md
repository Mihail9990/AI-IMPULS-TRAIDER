# Bybit TradFi / MetaTrader 5

Единственная текущая версия проекта — стратегия TradFi Recovery для `XAUUSD.s`,
счёта UST hedging и прямого подключения через официальный Python-пакет MetaTrader5.
Точка входа: **`tradfi_main.py`**. Полная логика, ограничения и исторические этапы
проверки описаны в [инструкции](docs/TRADFI_MT5_RU.md).

## Установка одним Python-файлом

Скачать `install_tradfi.py` из ветки `work` через **Download raw file** и открыть
Python 3.13 x64. Откроется окно с кнопками установки/обновления, настройки Telegram,
READ_ONLY и rollback кода. Ручные CMD и ZIP не нужны. Постоянные данные отделены от версий
кода в `%LOCALAPPDATA%\AI_IMPULS_TRADFI_Manager`. Подробная
[инструкция загрузчика](docs/WINDOWS_INSTALLER_RU.md).

## Windows VPS: установка и read-only запуск

Нужны Windows, 64-bit Python 3.10+ и установленный MetaTrader 5 с выбранным счётом.
В CMD из папки проекта:

```bat
py -m pip install -r requirements-tradfi-mt5.txt
```

Для новой установки скопировать `tradfi_config.example.json` в `service.json` и
указать локально точные terminal_path, expected_server и положительный целочисленный
expected_login. Сохранить `enable_trading=false` и оба legacy-флага `false` до сверки.
Для существующей установки сохранить её конфигурацию и SQLite, не заменять базу примером.

Telegram-настройки хранятся вне проекта: скопировать `telegram.example.json` в
`%LOCALAPPDATA%\AI_IMPULS_TRADFI\telegram.json`, заполнить три значения локально.
В `service.json` добавить:

```json
"telegram_credentials_file": "%LOCALAPPDATA%/AI_IMPULS_TRADFI/telegram.json"
```

Файл — незашифрованный JSON; ограничить доступ своей Windows-учётной записью вручную.
Не отправлять token, конфигурацию или SQLite в чат или репозиторий. Переменные
`TRADFI_TELEGRAM_TOKEN`, `TRADFI_TELEGRAM_CHAT_ID`, `TRADFI_TELEGRAM_USER_ID`
имеют приоритет над файлом по каждому полю. `.env` автоматически не загружается.
Относительный путь credentials разрешается относительно файла конфигурации.

После проверки отсутствия другой копии сервиса:

```bat
py tradfi_main.py --mode mt5 --service --config service.json
```

Это **READ_ONLY**, а не симуляция: доступны Telegram и наблюдения MT5, `/start`
отклоняется. Никаких торговых разрешений программа не включает.
`check_mt5.cmd` запускает отдельную read-only диагностику, не постоянный сервис.

## Граница реального исполнения

Торговля требует одновременно `--service --execute`, JSON `enable_trading=true`,
`execution_contract=cycle_scoped_identity_v1`, точной identity счёта,
`legacy_ea_removed=true`, `legacy_state_reconciled=true` и разрешений MT5.
Legacy-флаги относятся к прежнему MT5 EA и его обязательствам: наличие пустой новой
базы или успешного экспорта исторической сделки не доказывает сверку старого состояния.
Сохранены запреты повторной отправки UNKNOWN и автоматического переноса старой базы.

В пустом IDLE цикл разрешается через `/start`; активное сохранённое состояние после
перезапуска может продолжиться без новой команды. Без `/stop` после завершения
действует автоматический следующий цикл. `/stop` после исполнения завершает текущий
логический цикл, затем ждёт `/start`; это не аварийное закрытие позиции.
Recovery не ограничивается числом веток. Через 24 часа — MANUAL с сохранением
позиций и ордеров. Подробности и ограничения — в основной инструкции.

## Offline-проверки

```bat
py tradfi_main.py --mode simulate
py -m unittest discover -s tests -v
```

Симулятор и тесты используют искусственного брокера. Native fixture проверяет
структуру и корреляцию полей, не подтверждает ownership, исторический UTC или
реальное исполнение. Для C++-проверок EA JSON требуется g++; без него они пропускаются.

## Файлы

- `tradfi/` — стратегия, MT5 adapter, SQLite ledger, Telegram и диагностика.
- `tools/` — read-only проверки времени, истории и экспорт закрытой цепочки.
- `mt5/` — сохранённый прежний EA и read-only script для разбора legacy mailbox.
  Для прямого Python-сервиса EA подключать не требуется.
- `docs/` — текущая инструкция и описание диагностических инструментов.
- `tests/` — offline-регрессии TradFi/MT5 и диагностических инструментов.

Старая реализация Capital.com удалена из текущего дерева. История Git сохранена.
Пользовательские диагностические файлы и прежние локальные базы автоматически не удаляются.
