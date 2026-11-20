import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {test} from 'node:test';
import {wireSupportSettings} from './support_controls.mjs';

test('single threshold edits exact text only while enabled', () => {
  const events = new Map(), threshold = {}, save = {}, payloads = [];
  const form = {
    addEventListener(name, callback) { events.set(name, callback); },
    removeEventListener(name, callback) { if (events.get(name) === callback) events.delete(name); },
    querySelector: () => save,
    elements: {namedItem: (name) => name === 'support_threshold' ? threshold : undefined},
  };
  const submit = () => events.get('submit')?.({preventDefault() {}});
  const control = wireSupportSettings(form, {initial: {support_threshold: '0.40'},
    editable: false, onSave: (payload) => payloads.push(payload)});
  assert.equal(threshold.value, '0.40');
  assert.equal(threshold.disabled, true);
  assert.equal(save.disabled, true);
  submit();
  control.setEditable(true);
  threshold.value = '0.450';
  submit();
  control.setEditable(false);
  submit();
  control.disconnect();
  control.setEditable(true);
  submit();
  assert.deepEqual(payloads, [{support_threshold: '0.450'}]);
});

test('markup exposes just the entry relocation price threshold', () => {
  const html = readFileSync(new URL('./support_controls.html', import.meta.url), 'utf8');
  assert.deepEqual([...html.matchAll(/<input\s+name="([^"]+)"/gu)].map((m) => m[1]),
    ['support_threshold']);
  assert.match(html, /На цену входа|на цену входа/u);
  assert.match(html, /placeholder="0\.40"/u);
  assert.doesNotMatch(html, /checkbox|0\.25|support_first/u);
});
