import assert from 'node:assert/strict';
import test from 'node:test';
import {mountSupportSettings} from './support_panel.mjs';

class Control {
  events = new Map();
  addEventListener(name, fn) { this.events.set(name, fn); }
  removeEventListener(name, fn) { if (this.events.get(name) === fn) this.events.delete(name); }
  fire(name) { return this.events.get(name)?.({preventDefault() {}}); }
}

const state = (revision = 0, editable = true, support_threshold = '0.40') =>
  ({ok: true, status: 200, json: async () => ({revision, editable, support_threshold})});
const fail = (status) => ({ok: false, status});

function fixture(...responses) {
  const refresh = new Control(), output = new Control(), save = new Control(), input = new Control();
  const form = new Control(), calls = [];
  form.elements = {namedItem: (name) => name === 'support_threshold' ? input : undefined};
  form.querySelector = (selector) => ({
    '[data-support-refresh]': refresh, '[data-support-status]': output, 'button[type="submit"]': save,
  }[selector]);
  const api = mountSupportSettings(form, {request: async (url, init) => {
    calls.push({url, ...init});
    return responses.shift()?.();
  }});
  return {form, refresh, output, save, input, calls, api};
}

test('missing owner stays blank and readonly, does not send any PUT', async () => {
  const f = fixture(() => fail(503));
  assert.equal(f.save.disabled, true);
  await f.form.fire('submit');
  await f.api.refresh();
  assert.equal(f.input.value, '');
  assert.equal(f.save.disabled, true);
  assert.match(f.output.textContent, /недоступна/u);
  await f.form.fire('submit');
  assert.deepEqual(f.calls.map((r) => r.method), ['GET']);
});

test('edit saves exact text and matching owner revision', async () => {
  const f = fixture(() => state(2), () => state(3, true, '0.450'));
  await f.api.refresh();
  assert.equal(f.save.disabled, false);
  f.input.value = '0.450';
  await f.form.fire('submit');
  assert.deepEqual(f.calls.map((r) => r.method), ['GET', 'PUT']);
  assert.equal(f.calls[1].url, '/api/recovery/ea/support-settings');
  assert.deepEqual(JSON.parse(f.calls[1].body), {revision: 2, support_threshold: '0.450'});
  assert.equal(f.input.value, '0.450');
  assert.match(f.output.textContent, /сохранён/u);
});

test('conflicting state refreshes read-only, never auto-retries the save', async () => {
  const f = fixture(() => state(2), () => fail(409), () => state(3, false));
  await f.api.refresh();
  f.input.value = '0.450';
  await f.form.fire('submit');
  assert.equal(f.save.disabled, true);
  assert.match(f.output.textContent, /не сохранена/u);
  assert.deepEqual(f.calls.map((r) => r.method), ['GET', 'PUT', 'GET']);
});

test('lost save response disables controls until manual re-read', async () => {
  const f = fixture(() => state(), () => Promise.reject(new Error('network')), () => state(1));
  await f.api.refresh();
  await f.form.fire('submit');
  await f.form.fire('submit');
  assert.equal(f.save.disabled, true);
  assert.match(f.output.textContent, /не подтверждено/u);
  await f.refresh.fire('click');
  assert.equal(f.save.disabled, false);
  assert.deepEqual(f.calls.map((r) => r.method), ['GET', 'PUT', 'GET']);
  f.api.disconnect();
  await f.form.fire('submit');
  assert.equal(f.calls.length, 3);
});
