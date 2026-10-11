"""Separate TradFi Telegram adapter; no reuse of Capital trading commands."""
import queue
import threading
import time
import requests
from .settings import Settings


class Telegram:
    COMMANDS = [{'command': c, 'description': d} for c, d in (
        ('settings', 'Настройки нового логического цикла'), ('start', 'Разрешить цикл TradFi'),
        ('stop', 'Остановить после логического цикла'), ('status', 'Состояние TradFi'))]

    def __init__(self, token, chat_id, session=None):
        self.base = 'https://api.telegram.org/bot' + token
        self.chat_id = str(chat_id)
        self.http = session or requests.Session()
        self.incoming = queue.Queue(maxsize=100)
        self.outgoing = queue.Queue(maxsize=100)
        self.acks = queue.Queue()
        self.stop_event = threading.Event()
        self.threads = []

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
        self.threads = [threading.Thread(target=f, daemon=True) for f in (poll, send)]
        for thread in self.threads: thread.start()

    def process(self, controller):
        while True:
            try: update = self.incoming.get_nowait()
            except queue.Empty: break
            uid = int(update['update_id'])
            if uid < controller.state['telegram_offset']:
                continue
            message = update.get('message', {})
            if str(message.get('chat', {}).get('id')) == self.chat_id and isinstance(message.get('text'), str):
                try:
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

    def close(self):
        self.stop_event.set()
        for thread in self.threads: thread.join(timeout=6)
