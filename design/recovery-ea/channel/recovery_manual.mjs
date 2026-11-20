const API = '/api/recovery/ea';
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const isUuid = (value) => typeof value === 'string' && value.length === 36 && UUID.test(value);
const MODES = Object.freeze({
  ready: 'EA готов принять команду ожидания.',
  manual_wait: 'EA ждёт ручную позицию.',
  pending_wait: 'EA ждёт ручной триггер-ордер.',
  active: 'RECOVERY сопровождает позицию.',
  unknown: 'EA не подтвердил доступность.',
});
const WAITING = new Set(['manual_wait', 'pending_wait']);
const STATES = new Set(['accepted', 'applied', 'rejected', 'unknown']);
const ACTIONS = ['manual_wait', 'manual_pending_wait', 'manual_cancel'];

export function wireRecoveryManual(section, {request, makeId = () => crypto.randomUUID()}) {
  const buttons = Object.fromEntries(ACTIONS.map((action) =>
    [action, section.querySelector(`[data-action="${action}"]`)]));
  const check = section.querySelector('[data-manual-check]');
  const cancelNote = section.querySelector('[data-cancel-note]');
  const output = section.querySelector('[data-manual-status]');
  if (![...Object.values(buttons), check, cancelNote, output].every(Boolean) ||
      typeof request !== 'function' || typeof makeId !== 'function') {
    throw new TypeError('Incomplete manual RECOVERY panel');
  }

  let state = null;
  let current = null;
  let busy = false;
  let disposed = false;

  function message(text) { if (!disposed) output.textContent = text; }

  function allowed(action) {
    return action === 'manual_cancel' ? WAITING.has(state?.mode) : state?.mode === 'ready';
  }

  function render() {
    if (disposed) return;
    for (const action of ACTIONS) buttons[action].disabled = busy || current !== null || !allowed(action);
    buttons.manual_cancel.hidden = cancelNote.hidden = !allowed('manual_cancel');
    check.disabled = busy;
  }

  async function readState() {
    const response = await request(API + '/state', {method: 'GET', cache: 'no-store'});
    if (!response.ok) throw new Error('state unavailable');
    const value = await response.json();
    if (value?.version !== 1 || ![value.boot_id, value.context_id, value.generation_id].every(isUuid) ||
        typeof value.mode !== 'string' || !Object.hasOwn(MODES, value.mode)) throw new Error('invalid state');
    return value;
  }

  async function handleStatus(value, entry) {
    if (value?.request_id !== entry.ticket.request_id || value.retry_allowed !== false ||
        !STATES.has(value.status) || !['published', 'unknown'].includes(value.dispatch) ||
        (value.dispatch !== 'published' && value.status !== 'unknown')) {
      throw new Error('invalid receipt');
    }
    if (value.status === 'applied' || value.status === 'rejected') {
      current = null;
      state = null;
      // A receipt concerns arming or cancelling waiting, never confirms an SL.
      const action = value.status === 'rejected' ? 'EA отклонил команду.'
        : entry.action === 'manual_cancel' ? 'EA отменил ожидание.'
        : 'EA начал ожидание.';
      message(action);
      try {
        const next = await readState();
        if (disposed) return;
        state = next;
        message(action + ' ' + MODES[next.mode]);
      } catch {
        message(action + ' Текущее состояние пока недоступно.');
      }
    } else if (value.status === 'accepted') {
      message('EA принял команду. Смена режима ещё не подтверждена.');
    } else {
      message(value.dispatch === 'published'
        ? 'Команда записана в очередь. EA ещё не подтвердил смену режима.'
        : 'Статус команды неизвестен. Проверь состояние, не отправляй повторно.');
    }
  }

  async function refresh() {
    if (disposed || busy) return;
    busy = true;
    render();
    const entry = current;
    try {
      if (entry) {
        const {request_id, expected_boot} = entry.ticket;
        const response = await request(API + '/commands/' + request_id + '?expected_boot=' + expected_boot,
          {method: 'GET', cache: 'no-store'});
        if (!response.ok) throw new Error('status unavailable');
        const status = await response.json();
        if (disposed) return;
        await handleStatus(status, entry);
        if (status.status !== 'unknown' || disposed) return;
        const next = await readState();
        if (disposed) return;
        if (next.boot_id !== expected_boot) {
          current = null;
          state = next;
          message('EA начал новый сеанс. Прошлая команда не переносится. ' + MODES[next.mode]);
        }
      } else {
        const next = await readState();
        if (disposed) return;
        state = next;
        message(MODES[next.mode]);
      }
    } catch {
      if (!disposed && !entry) state = null;
      message(entry ? 'Не удалось проверить ответ EA. Команда повторно не отправлялась.'
        : 'Не удалось получить состояние EA.');
    } finally {
      busy = false;
      render();
    }
  }

  async function submit(action) {
    if (disposed || busy || current || !allowed(action)) return;
    let requestId;
    try {
      requestId = makeId();
      if (!isUuid(requestId)) throw new TypeError();
    } catch {
      message('Не удалось создать идентификатор команды. Команда не отправлена.');
      return;
    }
    const ticket = {request_id: requestId, expected_boot: state.boot_id,
      expected_context: state.context_id, expected_generation: state.generation_id};
    current = {action, ticket};
    const entry = current;
    busy = true;
    render();
    message('Отправка команды…');
    try {
      const response = await request(API + '/' + action, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...ticket, confirmed: true}),
      });
      if (disposed) return;
      if (response.status === 422) {
        current = null;
        state = null;
        message('Сервер не принял команду. Проверь состояние.');
        return;
      }
      if (!response.ok) throw new Error('submission failed');
      const status = await response.json();
      if (!disposed) await handleStatus(status, entry);
    } catch {
      message('Ответ на отправку не получен. Проверь состояние; повторной отправки не было.');
    } finally {
      busy = false;
      render();
    }
  }

  const actions = ACTIONS.map((action) => {
    const callback = () => submit(action);
    buttons[action].addEventListener('click', callback);
    return [action, callback];
  });
  check.addEventListener('click', refresh);
  render();

  return {
    refresh,
    disconnect() {
      disposed = true;
      for (const [action, callback] of actions) {
        buttons[action].removeEventListener('click', callback);
        buttons[action].disabled = true;
      }
      check.removeEventListener('click', refresh);
    },
  };
}
