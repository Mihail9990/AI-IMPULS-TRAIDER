import assert from 'node:assert/strict';
import test from 'node:test';
import {buildRangePayload, wireRecoveryRanges} from './recovery_ranges.mjs';

const make = (digit) => digit.repeat(8) + '-' + digit.repeat(4) + '-' +
  digit.repeat(4) + '-' + digit.repeat(4) + '-' + digit.repeat(12);
const ID = make('4');
const BOOT = make('1');
const api = '/api/recovery/ea';

class Element {
  constructor(value) { this.value = value; this.events = new Map(); }
  addEventListener(name, fn) { this.events.set(name, fn); }
  removeEventListener(name, fn) {
    if (this.events.get(name) === fn) this.events.delete(name);
  }
  invoke(name) { return this.events.get(name)?.({preventDefault() {}}); }
}

const state = (boot = BOOT, mode = 'ready') =>
  ({boot_id: boot, context_id: make('2'), generation_id: make('3'), version: 1, mode});
const status = (status, dispatch = 'published') =>
  ({request_id: ID, status, dispatch, retry_allowed: false});
const ok = (json) => ({ok: true, status: 200, json: async () => json});

function mount(...responses) {
  const fields = Object.fromEntries(Object.entries({
    range_count: '1', side_1: 'SELL', side_2: 'BUY', price_1: '3999.850',
    price_2: '4000.00', send_ranges: '', check_ranges: '',
  }).map(([k, v]) => [k, new Element(v)]));
  const second = new Element();
  const output = new Element();
  const form = Object.assign(new Element(), {
    elements: {namedItem: (name) => fields[name]},
    querySelector: (selector) => selector === '[data-range-second]' ? second : output,
  });
  const calls = [];
  const adapter = wireRecoveryRanges(form, {makeId: () => ID, request: async (url, init) => {
    calls.push({url, ...init});
    const next = responses.shift();
    assert.ok(next, 'unexpected request');
    return await next();
  }});
  return {fields, second, output, form, calls, adapter,
    refresh: () => fields.check_ranges.invoke('click'),
    submit: () => form.invoke('submit')};
}

test('strict prices and chosen sides are preserved exactly', () => {
  const first = {side: 'SELL', price: '3999.850'};
  const second = {side: 'BUY', price: '4000.00'};
  assert.deepEqual(buildRangePayload('1', [first, {side: '', price: ''}]),
    {ranges: [{range_id: 1, ...first}]});
  assert.deepEqual(buildRangePayload('2', [first, second]),
    {ranges: [{range_id: 1, ...first}, {range_id: 2, ...second}]});
  for (const price of ['0.000', '1e3', '01', '-1', '4000,0', '1\n', '1.23456789012345678', 4000]) {
    assert.throws(() => buildRangePayload('1', [{...first, price}]), /цену/u);
  }
  assert.throws(() => buildRangePayload('1', [{...first, side: 'sell'}]), /BUY/u);
});

test('explicit submit uses captured context and accepted is not applied', async () => {
  const m = mount(() => ok(state()), () => ok(status('accepted')), () => ok(status('applied')));
  assert.equal(m.fields.send_ranges.disabled, true);
  assert.equal(m.second.hidden, true);
  await m.submit();
  assert.equal(m.calls.length, 0);
  await m.refresh();
  assert.equal(m.fields.send_ranges.disabled, false);
  m.fields.range_count.value = '2';
  m.fields.range_count.invoke('change');
  assert.equal(m.second.hidden, false);
  assert.equal(m.fields.price_2.required, true);
  await m.submit();
  assert.equal(m.calls[1].url, api + '/configure_ranges');
  assert.deepEqual(JSON.parse(m.calls[1].body), {
    request_id: ID, expected_boot: BOOT, expected_context: make('2'), expected_generation: make('3'),
    confirmed: true, payload: {ranges: [
      {range_id: 1, side: 'SELL', price: '3999.850'}, {range_id: 2, side: 'BUY', price: '4000.00'},
    ]},
  });
  assert.match(m.output.textContent, /принял.*не подтверждено/u);
  assert.equal(m.fields.send_ranges.disabled, true);
  await m.refresh();
  assert.match(m.output.textContent, /выполнена/u);
  assert.equal(m.calls[2].url, `${api}/commands/${ID}?expected_boot=${BOOT}`);
  assert.equal(m.calls[2].method, 'GET');
});

test('response lost: double-submit and repeated checks never retry POST', async () => {
  let reject;
  const post = new Promise((_, fail) => { reject = fail; });
  const m = mount(() => ok(state()), () => post,
    () => ok(status('unknown', 'unknown')), () => ok(state()), () => ok(status('accepted')));
  await m.refresh();
  const first = m.submit();
  await m.submit();
  await m.refresh();
  assert.equal(m.calls.length, 2);
  reject(new Error('response lost'));
  await first;
  assert.match(m.output.textContent, /повторной отправки не было/u);
  await m.submit();
  await m.refresh();
  await m.refresh();
  assert.deepEqual(m.calls.map((c) => c.method), ['GET', 'POST', 'GET', 'GET', 'GET']);
  assert.equal(m.calls[2].url, m.calls[4].url);
  assert.match(m.calls[4].url, new RegExp(ID));
  assert.match(m.output.textContent, /принял.*не подтверждено/u);
});

test('new EA boot discards local pending status but never resubmits', async () => {
  const m = mount(() => ok(state()), () => ok(status('unknown', 'unknown')),
    () => ok(status('unknown', 'unknown')), () => ok(state(make('5'))));
  await m.refresh();
  await m.submit();
  await m.refresh();
  assert.equal(m.fields.send_ranges.disabled, false);
  assert.match(m.output.textContent, /новый сеанс/u);
  assert.equal(m.calls.filter((c) => c.method === 'POST').length, 1);
});

test('bad identity and disposed forms remain read-only', async () => {
  let resolve;
  const get = new Promise((r) => { resolve = r; });
  const m = mount(() => ok(state(BOOT + '\n')), () => get);
  await m.refresh();
  assert.equal(m.fields.send_ranges.disabled, true);
  const pending = m.refresh();
  m.adapter.disconnect();
  await m.submit();
  resolve(ok(state()));
  await pending;
  assert.equal(m.fields.send_ranges.disabled, true);
  assert.equal(m.calls.length, 2);
});

test('server schema rejection is not an EA receipt', async () => {
  const m = mount(() => ok(state()), () => ({ok: false, status: 422}));
  await m.refresh();
  await m.submit();
  assert.match(m.output.textContent, /Сервер не принял форму/u);
  assert.equal(m.fields.send_ranges.disabled, true);
  assert.equal(m.fields.price_1.disabled, false);
});
