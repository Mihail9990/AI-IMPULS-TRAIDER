# Caddy companion для отдельного recovery-web

Подготовка, не запуск. Нет службы, автозапуска, firewall, изменения ACL, MT5,
копирования паролей/сертификатов или подключения к VPS.

Источник алгоритма проверки ZIP: чистый коммит AI-IMPULS-TRAIDER
`4813051656b26e9219cbf15550d10d6b57690608`, `https_setup.py::validate_archive`.
Перенесён настоящий старый порядок действий: официальный ZIP → выбор файла в GUI →
подтверждение → SHA256 + структура ZIP/CRC + PE AMD64 → подготовленный EXE.
Версия намеренно остаётся RECOVERY **2.11.9** с закреплёнными ZIP/EXE SHA256:
это совместимо с существующим лаунчером, который отвергает старую 2.11.7.
Старые службы/порты/учётные данные и миграция основного бота не перенесены.

## Для сборщика

`prepare_caddy(server_root, archive=None, confirmed=False)`:
- server_root должен быть отдельным `AI_IMPULS_RECOVERY_WEB`, уже созданным bootstrap;
- назначение `<root>/tools/caddy/caddy.exe`;
- при archive выбираются локальные байты, без сети;
- без archive разрешён только закреплённый официальный GitHub URL;
- существующий правильный EXE сохраняется, чужой/другой не перезаписывается;
- Caddy не исполняется даже для проверки версии;
- подтверждение требуется перед подготовкой.

GUI `PREPARE_RECOVERY_HTTPS.cmd` использует `%LOCALAPPDATA%/AI_IMPULS_RECOVERY_WEB`
и официальный ZIP, выбранный пользователем. Python3.13 с Tk необходим.
Скачивание через браузер:
https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_windows_amd64.zip

Запуском Caddy владеет только RECOVERY-лаунчер. Не добавлять параллельно
AIImpulsHTTPS/RecoveryWebHTTPS service: это второй владелец TCP443.
Native API остаётся loopback127.0.0.1:8767, сертификаты только прежнего отдельного
RECOVERY-профиля, firewall только через существующее отдельное opt-in действие.
Занятый TCP443 или иной владелец HTTPS требует остановки процесса установки и
диагностики, не автоматического вытеснения основного бота.

## Общий account/symbol lock

В чистом основном проекте `tradfi.runtime.account_lock_path` использует SHA256
`json.dumps([expected_server, expected_login, symbol], ensure_ascii=True).encode('ascii')`.
Windows-путь: `%LOCALAPPDATA%/AI IMPULS TradFi/locks/<digest>.lock`.
Не включать magic, продукт или путь БД в digest. Основной owner_lock блокирует
один байт с позиции0 через `msvcrt.LK_NBLCK` и держит handle всю жизнь runtime.
Новый RECOVERY должен занимать ТОТ ЖЕ lock до открытия native owner/scheduler и
держать до завершения. Только его собственный database.owner.lock недостаточен.
Это межпроцессная защита под одним Windows пользователем; разные пользователи
имеют разные LOCALAPPDATA и этим механизмом не взаимно исключаются.

Пять synthetic tests PASS. Windows GUI, официальное скачивание и запуск Caddy
здесь не выполнялись. Это не подтверждение HTTPS или торговой готовности.
