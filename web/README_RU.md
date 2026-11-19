# RECOVERY web action fork · offline draft

Основа: actual old MAIN `trader-analysis-controls/web/local-panel`, не basket.
Разделы панели: «Основная система», «Настройки» и «История».
Прежние API, торговые обработчики и синтетический график не переносились.
Исходная phase1-панель в recovery-web-work/web не изменена этой работой.

## Runtime assets

Host должен явно сервить7файлов (не tests/README): index.html,panel.css,
model.js,panel.js,controls.js,control-policy.mjs,recovery-actions.mjs.
ESmodules нужны JavaScript MIME и CSP script-src self; всё same-origin,
без внешних ресурсов/inlineJS/inlineCSS. Production HTTPS, WebLocks и
SubtleCrypto обязательны для управления; без них остаётся просмотр.

## Разделение

model.js/panel.js: только GET state и произвольного существующего UUID receipt,
masked-key в памяти, request nonce/no-store, textContent.
control-policy.mjs: pure input validation и UI capability projection.
recovery-actions.mjs: изолированный перенесённый safe transport draft,
добавлены обязательный top-level web_trading===true на обоих GET preflight,
minSL0.60, fixed initial0.01, запрет reconcile.
controls.js: handoff/operator checkboxes, preview modal, явные пользовательские
submit/click, status-only pending resolution. Timer вызывает только GET.

## Разрешение управления

Все условия одновременно:
1. Подтверждённый native snapshot/proof/schema, top-level web_trading строго true.
   Это backend обязан возвращать только в специально armed native process.
2. Пользователь лично проверил исход прежних команд. При неизвестном исходе
   флажок не отмечают. Флаг сервера не заменяет эту проверку.
3. Пользователь включил кнопки в этой вкладке. Оба checkbox изначально false,
   не сохраняются; logout/context-change сбрасывает их.
4. Соответствующая server capability, отсутствие unresolved веб-pending,
   рабочие storage/WebLocks. Перед POST снова GET и проверка контекста/поколения.
5. Отдельное подтверждение конкретной команды в modal. Нет auto confirm.

Действия:1/2точные цены+BUY/SELL, initial/reentry distance, cancel ranges,
close/cancel каждого разрешённого slot. /reconcile намеренно запрещён даже
прямым execute. Он НЕ совместим с обычным receipt-контрактом без отдельной работы.

## Неопределённый результат

Перед единственным POST сохраняется recovery.native.pending.v1:UUID,action,
context,canonical payload SHA256,rangeIds. НЕТ ключа/цен/полного body/rawreceipt.
WebLocks serializes все вкладки. Timeout или неизвестный receipt блокирует
новые команды. Кнопка проверки исхода выполняет только GETstatus. Очистки
unknown или повторной отправки нет. Terminal receipt должен совпасть с UUID,
context и digest; partial additionally exactmixed range statuses. POST200 сам
по себе не считается подтверждением. Остановка/закрытие страницы не отменяет
уже отправленный запрос и не останавливает стратегию VPS.

## Backend requirements

GET /api/recovery/state: привязанный к nonce state_freshness, объект
состояния схемы 1 и флаг web_trading. Флаг разрешает веб-управление,
но не подтверждает исход прежних команд.
POST /ranges,/cancel,/ranges/{1|2}/close-cancel:совместимый native schema,
expected_context/expected_generation,UUID durable idempotency.
GET /commands/{UUID}:persisted canonical payload/context/phase/outcome.
Backend обязан независимо проверять armed mode/capabilities/checkpoints.
JS-условия не заменяют серверную авторизацию. Переход нового old MAIN backend
на этот contract требует отдельного интеграционного аудита.

## Tests / limits

`node --test tests/*.test.* recovery-actions.test.mjs` (офлайн).
`node --check panel.js`; `node --check controls.js`.
Проверены pure UI policy/DOM contract, proof/gates, UUIDjournal, partial/rejected,
timeout/no retry, changed context/generation/capability, serverflag downgrade,
explicit user confirm, readonly transport и запрет reconcile.
Не проверены real device/browser dialogs/WebLocks lifecycle/network disconnect,
реальный backend/MT5. Не deploy/publish/package. Реальных команд не отправлялось.
Нужен независимый audit до выпуска.
