// Local UI only: the authenticated parent panel owns command identity and I/O.
export function wireSupportSettings(form, {initial, editable, onSave}) {
  const enabled = form.elements.namedItem('support_first_enabled');
  const first = form.elements.namedItem('support_first');
  const second = form.elements.namedItem('support_second');
  const save = form.querySelector('button[type="submit"]');
  if (!enabled || !first || !second || !save || typeof onSave !== 'function') {
    throw new TypeError('Incomplete support settings form');
  }
  enabled.checked = initial.support_first_enabled !== false;
  first.value = initial.support_first;
  second.value = initial.support_second;
  let idle = editable === true;
  function update() {
    enabled.disabled = !idle;
    first.disabled = !idle || !enabled.checked;
    first.required = !first.disabled;
    second.disabled = save.disabled = !idle;
    second.required = idle;
  }
  function submit(event) {
    event.preventDefault();
    if (!idle) return;
    onSave({
      support_first_enabled: enabled.checked,
      support_first: first.value,
      support_second: second.value,
    });
  }
  enabled.addEventListener('change', update);
  form.addEventListener('submit', submit);
  update();
  return {
    setEditable(value) { idle = value === true; update(); },
    disconnect() {
      enabled.removeEventListener('change', update);
      form.removeEventListener('submit', submit);
    },
  };
}
