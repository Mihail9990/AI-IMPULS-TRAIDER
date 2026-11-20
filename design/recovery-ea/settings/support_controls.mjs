// Local UI only: the authenticated parent panel owns revisions and I/O.
export function wireSupportSettings(form, {initial, editable, onSave}) {
  const threshold = form.elements.namedItem('support_threshold');
  const save = form.querySelector('button[type="submit"]');
  if (!threshold || !save || typeof onSave !== 'function' ||
      typeof initial?.support_threshold !== 'string') {
    throw new TypeError('Incomplete support settings form');
  }
  threshold.value = initial.support_threshold;
  let idle = editable === true;
  function update() {
    threshold.disabled = save.disabled = !idle;
    threshold.required = idle;
  }
  function submit(event) {
    event.preventDefault();
    if (idle) return onSave({support_threshold: threshold.value});
  }
  form.addEventListener('submit', submit);
  update();
  return {
    setEditable(value) { idle = value === true; update(); },
    disconnect() { form.removeEventListener('submit', submit); },
  };
}
