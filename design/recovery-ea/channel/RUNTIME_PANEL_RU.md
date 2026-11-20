# Подключение панели RECOVERY к очереди EA

Это отдельная сборка для новой ветки, не запуск прежнего торгового бота.
FastAPI отдаёт страницу `/recovery`, её локальные статические файлы,
существующий вход и маршруты `/api/recovery/ea/...`. Нет импорта MT5,
отправки заявок из Python или автоматического выбора прежней панели.

- Используется существующая сессия `__Host-RecoverySession`: HTTPS,
  HttpOnly, SameSite=Strict; мутация требует проверку Origin и CSRF.
- Ключ вводят только для входа через HTTPS. Он не встроен в HTML/JS,
  не сохраняется в browser storage, не передаётся в URL. При обработке API
  внутренний Bearer заголовок добавляется только на сервере из проверенной
  сессии. Голый Bearer не является запасным входом к этому API.
- Каталог команд и квитанций не отдаётся по HTTP. Форма проверяет по
  `request_id` и `expected_boot` именно ту команду, которую отправили.
- Разрешены только нужные методы и маршруты. Если EA ещё не создал состояние,
  страница и вход доступны, но форма не получает разрешённый контекст для отправки.

## Настройки запуска (не для вставки в браузер)

Приватный лаунчер передаёт серверу `RECOVERY_PANEL_KEY` из нового профиля,
`RECOVERY_PANEL_ORIGIN` с точным публичным HTTPS origin без завершающего
слэша, `RECOVERY_SCOPE_ID` с текущим ID области EA и
`RECOVERY_CHANNEL_ROOT` с тем же локальным NTFS-каталогом команд, который
использует EA. Приватный корень и `sessions/` должны быть созданы заранее;
Python не переносит файлы прошлых установок. Секреты не помещать в Caddyfile.

Из корня проекта, в среде с FastAPI и Uvicorn:

```powershell
python -m uvicorn --app-dir design/recovery-ea/channel runtime_panel:app_from_env --factory --host 127.0.0.1 --port 8767 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
```

Один процесс нужен для cookie-сессий в памяти. Caddy на этом же сервере
остаётся HTTPS reverse proxy и передаёт исходный Host, протокол HTTPS и путь
без переписывания. Если прежний профиль Caddy раздаёт статику отдельно,
в HTTPS-блоке направить на **этот** локальный процесс точные пути:

```caddyfile
@recoveryEA path /recovery /recovery/assets/* /api/recovery/session/* /api/recovery/ea/*
handle @recoveryEA {
    reverse_proxy 127.0.0.1:8767
}
```

Если HTTPS-блок уже проксирует все пути на этот локальный порт, добавлять
правило не нужно. Не запускать новый веб-процесс одновременно со старым
на том же порту. Не публиковать локальный порт напрямую. Это схема
подключения, не изменение конфигурации действующего VPS.

В комплекте для запуска нужны эти файлы, исходные `web/session.js`
и `runtime/web_session.py`, а также существующие `channel.py`,
`web_handoff.py` и `recovery_ranges.*`.
Форма не включает настройки SL, историю сделок или другие возможности
предыдущей панели. Данные в тестах записывались только во временную очередь
с имитацией статуса EA. Запуск MT5 и HTTPS на VPS не проверялись.

Из корня проекта (POSIX-оболочка):

```sh
PYTHONPATH=.:design/recovery-ea/channel python -m unittest -v test_runtime_panel test_channel test_web_handoff
node --test design/recovery-ea/channel/test_recovery_ranges.mjs
```
