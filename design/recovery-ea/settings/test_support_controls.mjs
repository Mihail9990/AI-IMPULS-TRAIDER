import assert from 'node:assert/strict';
import {test} from 'node:test';
import {wireSupportSettings} from './support_controls.mjs';

function eventTarget() {
  const events = new Map();
  return {
    addEventListener(name, callback) { events.set(name, callback); },
    removeEventListener(name, callback) {
      if (events.get(name) === callback) events.delete(name);
    },
    send(name) { events.get(name)?.({preventDefault() {}}); },
  };
}

test('one transfer disables first input without erasing value or blocking second', () => {
  const enabled = eventTarget();
  const first = {};
  const second = {};
  const button = {};
  const form = {...eventTarget(), querySelector: () => button,
    elements: {namedItem(name) { return {
      support_first_enabled: enabled, support_first: first, support_second: second,
    }[name]; }}};
  const payloads = [];
  const control = wireSupportSettings(form, {
    initial: {support_first: '0.25', support_second: '0.40'},
    editable: true, onSave: (data) => payloads.push(data),
  });
  assert.equal(enabled.checked, true);
  assert.equal(first.disabled, false);
  enabled.checked = false;
  enabled.send('change');
  assert.equal(first.disabled, true);
  assert.equal(second.disabled, false);
  assert.equal(first.value, '0.25');
  second.value = '0.45';
  form.send('submit');
  assert.deepEqual(payloads, [{support_first_enabled: false, support_first: '0.25', support_second: '0.45'}]);
  enabled.checked = true;
  enabled.send('change');
  assert.equal(first.disabled, false);
  assert.equal(first.value, '0.25');
  control.setEditable(false);
  assert.equal(enabled.disabled, true);
  assert.equal(second.disabled, true);
  assert.equal(button.disabled, true);
  form.send('submit');
  control.disconnect();
  control.setEditable(true);
  form.send('submit');
  assert.equal(payloads.length, 1);
});
