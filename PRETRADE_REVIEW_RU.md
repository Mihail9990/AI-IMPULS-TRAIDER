# Проверка Windows pretrade delivery

База: 2125f3fb4e849b5b3fceb1e6cb5fab0c4642b835. Публикация нового кода не выполнялась.

Реализованы отдельный launcher без service/live маршрута; локальный payload с SHA256; проверка полного offline набора перед promotion; согласованная резервная SQLite и сохранение старого кода; сохранение существующих блокировок; диагностические subprocess с ограничением времени; редактированный отчёт конфигурации; allowlist read-only MT5; локальный MOCK preview панели.

Независимый reviewer проверил diff. Исправлены: сохранение уже referenced релиза при неопределённом исходе записи pointer; отказ при нулевом/неполном наборе тестов; явное закрытие резервного SQLite соединения. Дополнительно покрыты ошибки импорта MT5/shutdown без вывода секретов.

Проверки Linux: полный offline runner — 510 tests OK, blocked real network attempts: 0; Chromium — 21 tests OK. Проверка `python -I pretrade_checks.py mt5` на Linux возвращает WINDOWS_REQUIRED без импорта/подключения MT5. Исходники не содержат runtime DB, локальных конфигураций или credentials; архив формируется из tracked source.

На настоящем Windows/VPS НЕ проверены: Tk GUI, py launcher, msvcrt lock implementation, установка wheels, подключение MT5 и внешний UTC endpoint. Тесты используют fake bindings; нет торговых запросов. Локальная панель — MOCK, её preview не подключается к MT5. MT5_TIMESTAMP_SEMANTICS_UNVERIFIED не отключён; диагностический WARN не подтверждает торговую готовность. Диагностика старого lock и восстановление прежней установки исключены по уточнению пользователя.
