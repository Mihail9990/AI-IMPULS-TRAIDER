import {wireSupportSettings} from './support_controls.mjs';

const PATH = '/api/recovery/ea/support-settings';

function snapshot(value) {
  if (!value || !Number.isSafeInteger(value.revision) || value.revision < 0 ||
      typeof value.editable !== 'boolean' || typeof value.support_threshold !== 'string' ||
      !/^[0-9]{1,12}(?:\.[0-9]{1,16})?$/u.test(value.support_threshold) ||
      !/[1-9]/u.test(value.support_threshold) || /[\r\n]/u.test(value.support_threshold)) {
    throw new Error('Invalid settings snapshot');
  }
  return value;
}

// Initial and unavailable states stay disabled. The form never sends a write
// based on the range state endpoint or on the placeholder shown to the user.
export function mountSupportSettings(form, {request}) {
  const refreshButton = form.querySelector('[data-support-refresh]');
  const status = form.querySelector('[data-support-status]');
  if (!refreshButton || !status || typeof request !== 'function') {
    throw new TypeError('Incomplete support settings panel');
  }
  let saved = null;
  let controls;
  let busy = false;
  let disposed = false;

  function message(text) {
    if (!disposed) status.textContent = text;
  }

  function render() {
    if (disposed) return;
    controls?.setEditable(!busy && saved?.editable === true);
    refreshButton.disabled = busy;
  }

  function apply(value) {
    if (disposed) return;
    saved = value;
    controls?.disconnect();
    controls = wireSupportSettings(form, {initial: {support_threshold: saved?.support_threshold ?? ''},
      editable: !busy && saved?.editable === true, onSave: save});
    render();
  }

  async function read() {
    const response = await request(PATH, {method: 'GET', cache: 'no-store'});
    if (!response.ok) throw new Error('Unavailable support settings');
    return snapshot(await response.json());
  }

  async function refresh() {
    if (disposed || busy) return;
    busy = true;
    render();
    try {
      const value = await read();
      apply(value);
      message(value.editable ? '' : 'Порог можно изменить, когда RECOVERY не активен.');
    } catch {
      apply(null);
      message('Настройка порога пока недоступна.');
    } finally {
      busy = false;
      render();
    }
  }

  async function save(payload) {
    if (disposed || busy || saved?.editable !== true) return;
    busy = true;
    const revision = saved.revision;
    render();
    try {
      const resp = await request(PATH, {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({revision, ...payload}),
      });
      if (resp.status === 409) {
        const value = await read();
        apply(value);
        message('Состояние изменилось. Настройка не сохранена.');
      } else if (resp.status === 422) {
        message('Введи положительный порог через точку с учётом шага цены.');
      } else if (resp.ok) {
        apply(snapshot(await resp.json()));
        message('Порог сохранён.');
      } else {
        throw new Error('Settings save unavailable');
      }
    } catch {
      apply(null);
      message('Сохранение не подтверждено. Обнови настройку.');
    } finally {
      busy = false;
      render();
    }
  }

  refreshButton.addEventListener('click', refresh);
  apply(null);
  return {
    refresh,
    disconnect() {
      disposed = true;
      controls?.setEditable(false);
      controls?.disconnect();
      refreshButton.removeEventListener('click', refresh);
    },
  };
}
