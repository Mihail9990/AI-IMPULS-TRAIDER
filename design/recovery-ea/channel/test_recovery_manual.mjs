import assert from 'node:assert/strict';
import test from 'node:test';
import {readFileSync} from 'node:fs';
import {wireRecoveryManual} from './recovery_manual.mjs';

const make = (digit) => digit.repeat(8) + '-' + digit.repeat(4) + '-' +
  digit.repeat(4) + '-' + digit.repeat(4) + '-' + digit.repeat(12);
const ID = make('4');
const BOOT = make('1');
const api = '/api/recovery/ea';

class Element {
  events = new Map();
  addEventListener(name, fn) { this.events.set(name, fn); }
  removeEventListener(name, fn) {
    if (this.events.get(name) === fn) this.events.delete(name);
  }
  click() { return this.events.get('click')?.(); }
}

const state = (mode = 'ready', boot = BOOT) =>
  ({boot_id: boot, context_id: make('2'), generation_id: make('3'), version: 1, mode});
const status = (status, dispatch = 'published') =>
  ({request_id: ID, status, dispatch, retry_allowed: false});
const ok = (json) => ({ok: true, status: 200, json: async () => json});

function mount(...responses) {
  const buttons = Object.fromEntries(['manual_wait','manual_pending_wait','manual_cancel']
    .map((name) => [name, new Element()]));
  const check = new Element();
  const note = new Element();
  const output = new Element();
  const section = {
    querySelector: (selector) => ({
      '[data-manual-check]': check, '[data-manual-status]': output, '[data-cancel-note]': note,
      ...Object.fromEntries(Object.entries(buttons).map(([name, b]) => [`[data-action="${name}"]`, b])),
    }[selector]),
  };
  const calls = [];
  const adapter = wireRecoveryManual(section, {makeId: () => ID, request: async (url, init) => {
    calls.push({url, ...init});
    const next = responses.shift();
    assert.ok(next, 'unexpected request');
    return await next();
  }});
  return {buttons, check, note, output, adapter, calls};
}

test('markup has two arm buttons, contextual cancellation, and no range fields', () => {
  const html = readFileSync(new URL('./recovery_manual.html', import.meta.url), 'utf8');
  assert.deepEqual([...html.matchAll(/data-action="([^"]+)"/gu)].map((m) => m[1]),
    ['manual_wait', 'manual_pending_wait', 'manual_cancel']);
  assert.match(html, /Ордер и SL остаются/u);
  assert.doesNotMatch(html, /configure_ranges|Диапазон|<input/u);
});

test('pending arm uses exact empty request; accepted and applied never claim applied SL', async () => {
  const m = mount(() => ok(state()), () => ok(status('accepted')), () => ok(status('applied')),
    () => ok(state('pending_wait')));
  assert.equal(m.buttons.manual_wait.disabled, true);
  assert.equal(m.buttons.manual_cancel.hidden, true);
  await m.buttons.manual_pending_wait.click();
  assert.equal(m.calls.length, 0);
  await m.check.click();
  assert.equal(m.buttons.manual_pending_wait.disabled, false);
  await m.buttons.manual_pending_wait.click();
  assert.equal(m.calls[1].url, api + '/manual_pending_wait');
  assert.deepEqual(JSON.parse(m.calls[1].body), {
    request_id: ID, expected_boot: BOOT, expected_context: make('2'),
    expected_generation: make('3'), confirmed: true,
  });
  assert.match(m.output.textContent, /принял.*не подтверждена/u);
  await m.check.click();
  assert.match(m.output.textContent, /начал ожидание.*ждёт ручной триггер/u);
  assert.doesNotMatch(m.output.textContent, /SL/u);
  assert.equal(m.buttons.manual_cancel.hidden, false);
  assert.equal(m.note.hidden, false);
  assert.equal(m.buttons.manual_wait.disabled, true);
});

test('cancel only while waiting and posts no broker fields for both waiting modes', async () => {
  for (const mode of ['manual_wait', 'pending_wait']) {
    const m = mount(() => ok(state(mode)), () => ok(status('applied')), () => ok(state()));
    await m.check.click();
    assert.equal(m.buttons.manual_cancel.hidden, false);
    await m.buttons.manual_wait.click();
    assert.equal(m.calls.length, 1);
    await m.buttons.manual_cancel.click();
    assert.deepEqual(m.calls.map((c) => c.url), [api+'/state', api+'/manual_cancel', api+'/state']);
    assert.deepEqual(Object.keys(JSON.parse(m.calls[1].body)).sort(),
      ['confirmed', 'expected_boot', 'expected_context', 'expected_generation', 'request_id']);
    assert.match(m.output.textContent, /отменил ожидание/u);
    assert.equal(m.buttons.manual_cancel.hidden, true);
  }
});

test('manual market action sends its own operation and active has no cancel', async () => {
  const m = mount(() => ok(state()), () => ok(status('applied')), () => ok(state('active')));
  await m.check.click();
  await m.buttons.manual_wait.click();
  assert.deepEqual(m.calls.map((c) => c.url), [api+'/state', api+'/manual_wait', api+'/state']);
  assert.match(m.output.textContent, /сопровождает позицию/u);
  assert.equal(m.buttons.manual_cancel.hidden, true);
  assert.equal(m.buttons.manual_pending_wait.disabled, true);
});

test('uncertain submissions, duplicate taps, and boot change never re-POST', async () => {
  let reject;
  const initial = new Promise((_, fail) => { reject = fail; });
  const m = mount(() => ok(state()), () => initial, () => ok(status('unknown','unknown')),
    () => ok(state()), () => ok(status('unknown','unknown')), () => ok(state('ready', make('5'))));
  await m.check.click();
  const first = m.buttons.manual_pending_wait.click();
  await m.buttons.manual_pending_wait.click();
  assert.equal(m.calls.length, 2);
  reject(new Error('network'));
  await first;
  assert.match(m.output.textContent, /не получен/u);
  await m.buttons.manual_wait.click();
  await m.check.click();
  assert.equal(m.buttons.manual_wait.disabled, true);
  await m.check.click();
  assert.match(m.output.textContent, /новый сеанс/u);
  assert.equal(m.buttons.manual_wait.disabled, false);
  assert.equal(m.calls[2].url, `${api}/commands/${ID}?expected_boot=${BOOT}`);
  assert.deepEqual(m.calls.map((c) => c.method), ['GET','POST','GET','GET','GET','GET']);
});
