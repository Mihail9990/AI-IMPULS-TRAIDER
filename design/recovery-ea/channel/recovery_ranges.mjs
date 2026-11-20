const API = '/api/recovery/ea';
const DECIMAL = /^(?:0|[1-9][0-9]{0,14})(?:\.[0-9]{1,16})?$/u;
const UUID = /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u;
const isUuid = (value) => typeof value === 'string' && value.length === 36 && UUID.test(value);
const STATES = new Set(['accepted', 'applied', 'rejected', 'unknown']);
const ACTIVE = new Set(['ready', 'manual_wait', 'manual_cancel']);

export function buildRangePayload(count, input) {
  if (!['1', '2'].includes(count)) throw new TypeError('Выбери один или два диапазона.');
  return {ranges: [1, 2].slice(0, Number(count)).map((n) => {
    const {side, price} = input[n - 1];
    if (!['BUY', 'SELL'].includes(side)) throw new TypeError('Выбери BUY или SELL.');
    // The client sends the exact text; the EA owns actual tick-grid validation.
    if (typeof price !== 'string' || !DECIMAL.test(price) || /[\r\n]/u.test(price)
        || !/[1-9]/u.test(price)) throw new TypeError('Введи положительную цену через точку.');
    return {range_id: n, side, price};
  })};
}

/**
 * request must be the existing same-origin, authenticated panel fetch adapter,
 * including server-enforced CSRF for mutations. Do not place an internal bearer
 * key in this module or browser storage. makeId defaults to a new crypto UUID.
 */
export function wireRecoveryRanges(form, {request, makeId = () => crypto.randomUUID()}) {
  const field = (name) => form.elements.namedItem(name);
  const count = field('range_count');
  const side = [field('side_1'), field('side_2')];
  const price = [field('price_1'), field('price_2')];
  const send = field('send_ranges');
  const check = field('check_ranges');
  const second = form.querySelector('[data-range-second]');
  const output = form.querySelector('[data-range-status]');
  if (![count, ...side, ...price, send, check, second, output].every(Boolean) ||
      typeof request !== 'function' || typeof makeId !== 'function') {
    throw new TypeError('Incomplete RECOVERY form or request wiring');
  }

  let state = null;
  let current = null;
  let busy = false;
  let disposed = false;

  function message(text) {
    if (!disposed) output.textContent = text;
  }

  function render() {
    if (disposed) return;
    const edit = !busy && current === null;
    const two = count.value === '2';
    count.disabled = side[0].disabled = price[0].disabled = !edit;
    side[1].disabled = price[1].disabled = !edit || !two;
    price[0].required = edit;
    price[1].required = edit && two;
    second.hidden = !two;
    send.disabled = !edit || !state || !ACTIVE.has(state.mode);
    check.disabled = busy;
  }

  async function readState() {
    const response = await request(API + '/state', {method: 'GET', cache: 'no-store'});
    if (!response.ok) throw new Error('state request failed');
    const value = await response.json();
    if (value?.version !== 1 || ![value.boot_id, value.context_id, value.generation_id]
      .every(isUuid) ||
        ![...ACTIVE, 'unknown'].includes(value.mode)) throw new Error('invalid state');
    return value;
  }

  function applyStatus(value, ticket) {
    if (value?.request_id !== ticket.request_id || value.retry_allowed !== false ||
        !STATES.has(value.status) || !['published', 'unknown'].includes(value.dispatch) ||
        (value.dispatch !== 'published' && value.status !== 'unknown')) {
      throw new Error('Invalid receipt');
    }
    switch (value.status) {
      case 'applied':
        current = null;
        state = null;
        message('Команда выполнена EA. Для новой команды проверь состояние.');
        break;
      case 'rejected':
        current = null;
        state = null;
        message('EA отклонил команду. Для новой команды проверь состояние.');
        break;
      case 'accepted':
        message('EA принял команду. Выполнение ещё не подтверждено.');
        break;
      default:
        message(value.dispatch === 'published'
          ? 'Команда записана в очередь. EA ещё не подтвердил выполнение.'
          : 'Статус команды неизвестен. Проверь состояние, не отправляй повторно.');
    }
  }

  async function refresh() {
    if (disposed || busy) return;
    busy = true;
    render();
    const ticket = current;
    try {
      if (ticket !== null) {
        const url = API + '/commands/' + ticket.request_id + '?expected_boot=' + ticket.expected_boot;
        const response = await request(url, {method: 'GET', cache: 'no-store'});
        if (disposed) return;
        if (!response.ok) throw new Error('status request failed');
        const value = await response.json();
        if (disposed) return;
        applyStatus(value, ticket);
        if (value.status !== 'unknown') return;
        const observed = await readState();
        if (disposed) return;
        if (observed.boot_id !== ticket.expected_boot) {
          // Reset only local UI state; never resubmit the previous command.
          current = null;
          state = observed;
          message('EA начал новый сеанс. Прошлая команда не переносится.');
        }
      } else {
        const observed = await readState();
        if (disposed) return;
        state = observed;
        message(ACTIVE.has(observed.mode) ? 'Готово к отправке.' : 'EA не подтвердил доступность.');
      }
    } catch {
      if (!disposed && !ticket) state = null;
      message(ticket ? 'Не удалось проверить ответ EA. Команда повторно не отправлялась.'
        : 'Не удалось получить состояние EA.');
    } finally {
      busy = false;
      render();
    }
  }

  async function submit(event) {
    event.preventDefault();
    if (disposed || busy || current || !state || !ACTIVE.has(state.mode)) return;
    let payload;
    try {
      payload = buildRangePayload(count.value, side.map((input, i) =>
        ({side: input.value, price: price[i].value})));
    } catch (error) {
      message(error.message);
      return;
    }
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
    current = ticket;
    busy = true;
    render();
    message('Отправка команды…');
    try {
      const response = await request(API + '/configure_ranges', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({...ticket, confirmed: true, payload}),
      });
      if (disposed) return;
      if (response.status === 422) {
        current = null;
        state = null;
        message('Сервер не принял форму. Проверь значения и состояние EA.');
        return;
      }
      if (!response.ok) throw new Error('submission failed');
      const value = await response.json();
      if (!disposed) applyStatus(value, ticket);
    } catch {
      message('Ответ на отправку не получен. Проверь состояние; повторной отправки не было.');
    } finally {
      busy = false;
      render();
    }
  }

  form.addEventListener('submit', submit);
  count.addEventListener('change', render);
  check.addEventListener('click', refresh);
  render();

  return {
    refresh,
    disconnect() {
      disposed = true;
      form.removeEventListener('submit', submit);
      count.removeEventListener('change', render);
      check.removeEventListener('click', refresh);
    },
  };
}
