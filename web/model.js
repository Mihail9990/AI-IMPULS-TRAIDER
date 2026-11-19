(function (root) {
  'use strict';
  const fail = () => { throw new Error('Несовместимый или неподтверждённый ответ VPS.'); };
  const object = x => x !== null && typeof x === 'object' && !Array.isArray(x);
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const rows = x => Array.isArray(x) && x.length <= 10000 && x.every(object);
  function state(envelope, nonce) {
    if (object(envelope) && envelope.mode === 'read_only') {
      if (envelope.schema_version !== 1 || envelope.connection_only !== true ||
          envelope.live_trading !== false || envelope.can_cancel_ranges !== false ||
          envelope.web_trading === true || ['ranges','positions','orders','loss_history'].some(k =>
            !Array.isArray(envelope[k]) || envelope[k].length !== 0)) fail();
      return { connectionOnly: true, widget: null, age: null, webTrading: false };
    }
    if (!object(envelope) || !object(envelope.widget) || !object(envelope.state_freshness)) fail();
    const w = envelope.widget, proof = envelope.state_freshness, s = w.snapshot;
    if (w.schema_version !== 1 || w.mode !== 'native' || !object(s) || !['fresh','unavailable'].includes(s.status)) fail();
    if (proof.version !== 1 || !/^[a-f0-9]{32}$/.test(nonce) || proof.nonce !== nonce) fail();
    if (!(proof.age_ms === null || Number.isSafeInteger(proof.age_ms))) fail();
    if (!['real','demo','contest','unknown'].includes(w.account_trade_mode) || typeof s.context_id !== 'string' || !s.context_id || typeof s.symbol !== 'string') fail();
    if (!(s.generation === null || typeof s.generation === 'string') || typeof w.executor_ready !== 'boolean' || typeof w.reconciliation_required !== 'boolean') fail();
    if (!rows(w.ranges) || !object(w.settings) || !object(w.totals) || !Array.isArray(w.issues)) fail();
    if (s.status === 'fresh' && (!rows(w.positions) || !rows(w.orders))) fail();
    if (s.status === 'unavailable' && (w.positions !== null || w.orders !== null)) fail();
    return { widget: w, age: proof.age_ms, webTrading: envelope.web_trading === true }; // Age is diagnostic only. No wall-clock inference.
  }
  function spec(kind, id, nonce, key) {
    if (typeof key !== 'string' || key.length < 32 || !/^[\x20-\x7e]+$/.test(key)) throw new Error('Проверьте ключ доступа.');
    let path;
    if (kind === 'state') { if (!/^[a-f0-9]{32}$/.test(nonce)) fail(); path = '/api/recovery/state'; }
    else if (kind === 'receipt' && typeof id === 'string' && uuid.test(id)) path = '/api/recovery/commands/' + id;
    else throw new Error('Нужен существующий request_id в формате UUID.');
    const headers = { Authorization: 'Bearer ' + key, Accept: 'application/json', 'Cache-Control': 'no-store' };
    if (kind === 'state') headers['X-Recovery-State-Nonce'] = nonce;
    return { path, options: { method: 'GET', headers, cache: 'no-store', redirect: 'error', credentials: 'omit', referrerPolicy: 'no-referrer' } };
  }
  function receipt(data, id) {
    if (!object(data) || !object(data.command) || typeof data.context_id !== 'string' || data.command.request_id !== id) fail();
    const c = data.command;
    const labels = { not_recorded:'Запись не найдена. Это НЕ подтверждает, что команда не исполнится.', dispatching:'Исход не подтверждён. Команда ожидает сверки.', rejected:'Команда отклонена.', completed:'Обработка команды завершена.' };
    if (!Object.hasOwn(labels, c.phase)) fail();
    let message = labels[c.phase];
    if (c.phase === 'completed') {
      const outcomes = { confirmed:'Результат команды подтверждён.', partially_applied:'Применено частично. Проверяйте каждый диапазон.', reconciliation_required:'Обработка завершена, но результат требует сверки.' };
      if (!Object.hasOwn(outcomes,c.outcome)) fail(); message = outcomes[c.outcome];
    }
    if (c.phase === 'rejected' && c.outcome !== 'rejected') fail();
    return { message, outcomes: Array.isArray(c.range_outcomes) ? c.range_outcomes.filter(object) : [], context: data.context_id };
  }
  const api = { state, spec, receipt, uuid, text: x => (x === null || x === undefined || typeof x === 'object') ? '—' : String(x) };
  if (typeof module !== 'undefined' && module.exports) module.exports = api; else root.RecoveryModel = api;
})(typeof globalThis === 'undefined' ? this : globalThis);
