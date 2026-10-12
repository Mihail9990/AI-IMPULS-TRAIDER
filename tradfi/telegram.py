"""Separate TradFi Telegram adapter; no reuse of Capital trading commands."""
import queue
import threading
import time
import requests
from .settings import Settings


class Telegram:
    COMMANDS = [{'command': c, 'description': d} for c, d in (
        ('settings', 'Настройки нового логического цикла'), ('start', 'Разрешить цикл TradFi'),
        ('stop', 'Остановить после логического цикла'), ('status', 'Состояние TradFi'),
        ('sendlog', 'Безопасный диагностический лог файлом'),
        ('reset_manual', 'Завершить сверенный ручной цикл, ждать /start'))]

    def __init__(self, token, chat_id, session=None, *, user_id=None, log_path=None, document_session=None):
        self.base = 'https://api.telegram.org/bot' + token
        self.chat_id = str(chat_id)
        self.user_id = str(user_id) if user_id is not None else self.chat_id
        if not self.user_id.isdecimal() or int(self.user_id)<=0:
            raise ValueError('An explicit positive Telegram user ID is required for group chats')
        self.log_path = log_path
        self.http = session or requests.Session()
        self.incoming = queue.Queue(maxsize=100)
        self.outgoing = queue.Queue(maxsize=100)
        self.acks = queue.Queue()
        self.stop_event = threading.Event()
        self.threads = []
        self.documents = queue.Queue(maxsize=1)
        self.document_results = queue.Queue(maxsize=1)
        self._document_busy = False
        self._document_next = 0.
        self._document_notice_next = 0.
        self._document_http = document_session or requests.Session()
        self._owns_document_http = document_session is None

    @staticmethod
    def payload(chat_id, text):
        result = {'chat_id': str(chat_id), 'text': text}
        if text.startswith('/SETTINGS:'):
            # Telegram CopyTextButton supports at most 256 characters. The full settings
            # block is larger: send a selectable code block so the ENTIRE block is copied.
            result.update(parse_mode='HTML', text='<pre>' + text + '</pre>')
        return result

    def updates(self, offset):
        response = self.http.get(self.base + '/getUpdates',
                                 params={'offset': offset, 'timeout': 1}, timeout=5)
        response.raise_for_status()
        data = response.json()
        if not data.get('ok'):
            raise OSError('Telegram getUpdates rejected')
        return data['result']

    def start(self, offset):
        def poll():
            cursor = offset
            while not self.stop_event.is_set():
                try:
                    for update in self.updates(cursor):
                        if int(update['update_id']) < cursor:
                            continue
                        # Cursor moves only after the update is in the owner queue.
                        self.incoming.put(update, timeout=1)
                        cursor = int(update['update_id']) + 1
                except (requests.RequestException, OSError, ValueError, queue.Full):
                    self.stop_event.wait(1)
        def send():
            try:
                self.http.post(self.base + '/setMyCommands', json={'commands': self.COMMANDS}, timeout=5)
            except requests.RequestException:
                pass
            while not self.stop_event.is_set():
                try:
                    item = self.outgoing.get(timeout=.5)
                except queue.Empty:
                    continue
                try:
                    response = self.http.post(self.base + '/sendMessage',
                        json=self.payload(self.chat_id, item['text']), timeout=5)
                    response.raise_for_status()
                    if response.json().get('ok'):
                        self.acks.put(item['id'])
                except requests.RequestException:
                    pass  # owner retains durable outbox, retries with same report id
                finally:
                    self.outgoing.task_done()
        self.threads = [threading.Thread(target=f, daemon=True) for f in (poll, send, self._document_worker)]
        for thread in self.threads: thread.start()

    def process(self, controller):
        self._collect_document_results(controller)
        while True:
            try: update = self.incoming.get_nowait()
            except queue.Empty: break
            uid = int(update['update_id'])
            if uid < controller.state['telegram_offset']:
                continue
            message = update.get('message', {})
            if self._authorized(message) and isinstance(message.get('text'), str):
                try:
                    if message['text'].strip().lower()=='/sendlog':
                        self._request_document(controller,uid)
                    else:
                        controller.command(message['text'], uid)
                except (ValueError, OSError, TimeoutError) as exc:
                    controller.notify('Команда не принята: ' + str(exc))
                    controller.state['telegram_offset'] = uid + 1
                    controller.save('telegram_command_rejected')
            else:
                controller.state['telegram_offset'] = uid + 1
                controller.save('telegram_foreign_update_ignored')
        acknowledged = set()
        while True:
            try: acknowledged.add(self.acks.get_nowait())
            except queue.Empty: break
        if acknowledged:
            controller.state['notifications'] = [n for n in controller.state['notifications'] if n['id'] not in acknowledged]
            controller.save('telegram_outbox_delivered')
        # Limited resubmission interval; notifications may duplicate after a lost Telegram ack,
        # but a duplicate notification cannot replay a trading command.
        if time.monotonic() >= getattr(self, '_next_send', 0):
            for item in controller.state['notifications'][:20]:
                try: self.outgoing.put_nowait(dict(item))
                except queue.Full: break
            self._next_send = time.monotonic() + 10

    def _authorized(self, message):
        sender=message.get('from') or {}
        return (str((message.get('chat') or {}).get('id'))==self.chat_id
                and str(sender.get('id'))==self.user_id and sender.get('is_bot') is not True
                and not message.get('sender_chat'))

    @staticmethod
    def _document_notice(controller, text):
        # Keep only the latest diagnostics notice while preserving trading/manual alerts.
        controller.state['notifications']=[n for n in controller.state['notifications'] if n.get('topic')!='sendlog']
        controller.notify(text)
        controller.state['notifications'][-1]['topic']='sendlog'

    def _request_document(self, controller, uid):
        now=time.monotonic()
        controller.state['telegram_offset']=uid+1
        if self._document_busy or now<self._document_next:
            if now>=self._document_notice_next:
                self._document_notice(controller,'/sendlog уже обрабатывается или доступен раз в 60 секунд.')
                self._document_notice_next=now+60
            controller.save('telegram_sendlog_requested')
            return
        if self.log_path is None:
            self._document_notice(controller,'Диагностический лог TradFi не настроен; файл не отправлен.')
            controller.save('telegram_sendlog_requested')
            self._document_next=now+60
            return
        self._document_notice(controller,'/sendlog: подготовка безопасного лога поставлена в очередь.')
        # Persist offset before asynchronous handoff. Crash in this gap requires a new request;
        # old Telegram updates must never trigger a duplicate file upload after restart.
        controller.save('telegram_sendlog_requested')
        self._document_busy=True
        self._document_next=now+60
        self.documents.put_nowait(uid)

    def _document_worker(self):
        from .log_export import build_document
        try:
            while not self.stop_event.is_set():
                try:
                    uid=self.documents.get(timeout=.2)
                except queue.Empty:
                    continue
                outcome=False
                try:
                    payload=build_document(self.log_path)
                    if not self.stop_event.is_set():
                        response=self._document_http.post(self.base+'/sendDocument',
                            data={'chat_id':self.chat_id},
                            files={'document':('tradfi-diagnostics.log',payload,'text/plain; charset=utf-8')},
                            timeout=(3,15))
                        response.raise_for_status()
                        outcome=response.json().get('ok') is True
                except Exception:
                    # Neither exception strings nor request URLs (which contain the token) escape.
                    pass
                finally:
                    self.document_results.put_nowait((uid,outcome))
                    self.documents.task_done()
        finally:
            if self._owns_document_http: self._document_http.close()

    def _collect_document_results(self, controller):
        try:
            _,ok=self.document_results.get_nowait()
        except queue.Empty:
            return
        self._document_busy=False
        self._document_notice(controller,'Диагностический лог TradFi отправлен.' if ok else
            'Доставка лога не подтверждена. Повторите /sendlog через 60 секунд; автоматического повтора файла нет.')
        controller.save('telegram_sendlog_result')

    def close(self):
        if not self.threads and self._owns_document_http: self._document_http.close()
        self.stop_event.set()
        for thread in self.threads: thread.join(timeout=6)
