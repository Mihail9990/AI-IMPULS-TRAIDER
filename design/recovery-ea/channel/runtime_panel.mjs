import './session.js';
import {wireRecoveryManual} from './recovery_manual.mjs';
import {mountSupportSettings} from './support_panel.mjs';

const sessionApi = window.RecoverySession;
const login = document.getElementById('login');
const form = document.getElementById('login-form');
const keyInput = document.getElementById('panel-key');
const panel = document.getElementById('panel');
const logout = document.getElementById('logout');
const notice = document.getElementById('session-result');
let session = null;
let sessionBusy = false;

function show(next, text) {
  session = next;
  login.hidden = Boolean(session);
  panel.hidden = !session;
  notice.textContent = text;
}

async function panelRequest(path, init) {
  const current = session;
  if (!current) throw new Error('browser session unavailable');
  const method = init.method || 'GET';
  const headers = new Headers(init.headers);
  headers.set('X-Recovery-Session', '1');
  if (method === 'POST' || method === 'PUT') headers.set('X-Recovery-CSRF', current.csrf);
  headers.delete('Authorization');
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 15000);
  let response;
  try {
    response = await fetch(path, {
      ...init, headers, credentials: 'same-origin', cache: 'no-store',
      redirect: 'error', referrerPolicy: 'no-referrer', signal: controller.signal,
    });
  } finally {
    clearTimeout(timer);
  }
  if (response.status === 401 && session === current) {
    show(null, 'Сеанс закончился. Войди заново, чтобы проверить состояние.');
  }
  return response;
}

const manual = wireRecoveryManual(panel.querySelector('.recovery-manual'), {request: panelRequest});
const support = mountSupportSettings(panel.querySelector('.support-settings'), {request: panelRequest});

async function refreshPanel() {
  await Promise.all([manual.refresh(), support.refresh()]);
}


form.addEventListener('submit', async (event) => {
  event.preventDefault();
  if (sessionBusy || session) return;
  sessionBusy = true;
  const value = keyInput.value;
  keyInput.value = '';
  try {
    show(await sessionApi.login(value), '');
    await refreshPanel();
  } catch {
    show(null, 'Вход не подтверждён.');
  } finally {
    sessionBusy = false;
  }
});

logout.addEventListener('click', async () => {
  if (sessionBusy || !session) return;
  sessionBusy = true;
  try {
    await sessionApi.logout(session);
    show(null, 'Выход выполнен.');
  } catch {
    notice.textContent = 'Выход не подтверждён. Попробуй ещё раз.';
  } finally {
    sessionBusy = false;
  }
});

sessionBusy = true;
try {
  show(await sessionApi.restore(), '');
  await refreshPanel();
} catch {
  show(null, 'Войди в панель.');
} finally {
  sessionBusy = false;
}
