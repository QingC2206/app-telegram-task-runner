from __future__ import annotations
import json
import os
from datetime import datetime
import threading
import time
import traceback
import uuid
from .binding import load_config, parse_command, permitted, permitted_chat, permitted_message
from .executor import execute
from .locking import FileLock, BotLock
from .menus import MenuPublisher, commands_for
from .plugins import Registry
from .security import load_token
from .storage import Store
from .telegram import Telegram, ApiError
from .util import atomic_json, redact
from .health import interpret as interpret_health


class Host:
    def __init__(self, layout, config=None, api=None):
        self.layout = layout.ensure()
        self.config = config or load_config(layout)
        self.api = api or Telegram(load_token(self.config), self.config.get('proxy'))
        self.store = Store(layout.runtime / f"state-{self.config['bot_id']}-{self.config['chat_id']}.sqlite3")
        self.registry = Registry(layout).discover()
        self.stop = threading.Event()
        self.registry_lock = threading.RLock()
        self.menu_refresh = threading.Event()
        self.menu_publisher = MenuPublisher(self.api, self.store)
        self.workers = []
        self.started_at = time.time()
        self.health_lock = threading.RLock()
        self.telegram_health = {'last_poll_success_at':0,'last_error_at':0,'last_error':'','consecutive_failures':0}

    def health_snapshot(self, lifecycle='running'):
        with self.health_lock:
            value = {'schema_version':1,'pid':os.getpid(),'started_at':self.started_at,'heartbeat_at':time.time(),
                     'lifecycle':lifecycle,'workers':{thread.name:thread.is_alive() for thread in self.workers},
                     'telegram':dict(self.telegram_health)}
        return interpret_health(value)

    def poll_result(self, error=None):
        with self.health_lock:
            if error is None:
                self.telegram_health.update(last_poll_success_at=time.time(),consecutive_failures=0)
            else:
                self.telegram_health.update(last_error_at=time.time(),last_error=redact(error,self.api.token)[:500],
                                            consecutive_failures=self.telegram_health['consecutive_failures']+1)

    def health_loop(self):
        while not self.stop.is_set():
            try:
                atomic_json(self.layout.runtime / 'health.json', self.health_snapshot())
            except Exception as error:
                self.layout.log('Health snapshot failed: ' + str(error),self.api.token)
            self.stop.wait(5)

    def reply(self, key, text, message_id=None, *, chat_id=None):
        target = self.config['chat_id'] if chat_id is None else chat_id
        self.store.notify(key, target, redact(text, self.api.token), message_id)

    def menu(self, user_id=None):
        with self.registry_lock:
            return commands_for(self.config, self.registry, user_id)

    def handle(self, update):
        message = update.get('message') or {}
        user, chat = message.get('from') or {}, message.get('chat') or {}
        if not permitted_message(self.config, message):
            return
        if chat['type'] == 'private' and permitted(self.config, user['id'], 'admin'):
            seen = set(self.store.get('admin_private_chats', []))
            if chat['id'] not in seen:
                self.store.set('admin_private_chats', sorted(seen | {chat['id']}))
                self.menu_refresh.set()
        parsed = parse_command(message.get('text', ''), self.config['bot_username'])
        if not parsed:
            return
        command, argument = parsed
        key, source = 'update:' + str(update['update_id']), message.get('message_id')
        def reply(text):
            self.reply(key, text, source, chat_id=chat['id'])
        if time.time() - message.get('date', 0) > self.config.get('command_max_age_seconds', 900):
            if permitted(self.config, user['id']):
                reply('这条命令已过期，请重新发送。')
            return
        if command == 'whoami':
            reply(f"你的用户 ID：{user['id']}\n当前群/聊天 ID：{chat['id']}")
            return
        if command in ('start','help'):
            lines = ['Telegram Task Runner', '以下为你当前可用的命令。']
            if not permitted(self.config, user['id']):
                lines.append('执行任务需由管理员授权；先用 /whoami 获取自己的用户 ID。')
            lines.extend('/' + item['command'] + ' — ' + item['description'] for item in self.menu(user['id']))
            reply('\n'.join(lines)); return
        if not permitted(self.config, user['id']):
            return
        if command in ('allow', 'revoke', 'users'):
            if not permitted(self.config, user['id'], 'admin'):
                reply('此命令需要管理员权限。'); return
            if command == 'users':
                reply('授权用户：' + ', '.join(map(str, self.config['allowed_user_ids'])) + '\n管理员：' + ', '.join(map(str, self.config['admin_user_ids']))); return
            if not argument.isdigit() or int(argument) <= 0:
                reply('用法：/' + command + ' 用户ID'); return
            identifier = int(argument)
            if command == 'revoke' and identifier in self.config['admin_user_ids']:
                reply('管理员授权请通过本机 setup 管理。'); return
            users = set(self.config['allowed_user_ids'])
            users.add(identifier) if command == 'allow' else users.discard(identifier)
            config = {**self.config, 'allowed_user_ids': sorted(users)}
            atomic_json(self.layout.config, config)
            self.config = config
            self.menu_refresh.set()
            reply('已更新授权用户。'); return
        if command == 'status':
            health = self.health_snapshot()
            connection = {'online':'正常','retrying':'正在重连','connecting':'连接中','stale':'长时间未成功'}[health['telegram']['state']]
            last_poll = health['telegram']['last_poll_success_at']
            stamp = datetime.fromtimestamp(last_poll).astimezone().strftime('%H:%M:%S') if last_poll else '尚未确认'
            notices = self.store.notification_counts()
            unhealthy = [name for name,alive in health['workers'].items() if not alive]
            text = '🟢 宿主在线\nTelegram：' + connection + '\n最近成功连接：' + stamp
            text += '\n工作线程：' + ('异常 '+', '.join(unhealthy) if unhealthy else '正常')
            text += '\n任务状态：' + json.dumps(self.store.counts(),ensure_ascii=False) + '\n插件数量：' + str(len(self.registry.plugins))
            text += f"\n通知：已发送 {notices.get('sent',0)}，待发送 {notices.get('pending',0)+notices.get('retry',0)}，未确认 {notices.get('unknown',0)}，失败 {notices.get('failed',0)}"
            reply(text); return
        if command == 'plugins':
            with self.registry_lock:
                lines = []
                for plugin in self.registry.plugins.values():
                    names = [name for name,spec in plugin.manifest['commands'].items()
                             if permitted(self.config, user['id'], spec['permission'])]
                    if names:
                        lines.append(plugin.identifier + ' ' + plugin.manifest['version'] + '\n  /' + ' /'.join(names))
                text = '\n'.join(lines) or '没有你可用的插件命令。'
                if self.registry.errors:
                    text += '\n\n未加载：\n' + '\n'.join(k + ': ' + v for k,v in self.registry.errors.items())
            reply(text); return
        if command == 'jobs':
            reply('\n'.join(f"{j['id']} /{j['command']} {j['status']}" for j in self.store.jobs(chat_id=chat['id'])) or '还没有任务。'); return
        if command == 'job':
            job = self.store.job(argument.strip())
            if not job or job['chat_id'] != chat['id']:
                reply('没有找到这个任务，请先 /jobs。'); return
            text = f"任务：{job['id']}\n命令：/{job['command']}\n状态：{job['status']}\n进度：{job['progress'] or '等待执行'}"
            if job['result']:
                text += '\n\n' + json.loads(job['result'])['message']
            reply(text); return
        with self.registry_lock:
            self.registry.discover()
            target = self.registry.commands.get(command)
            if not target:
                reply('无法识别命令，请 /help 或 /plugins。'); return
            plugin, spec = target
            if not permitted(self.config, user['id'], spec['permission']):
                reply('没有执行此命令的权限。'); return
            if self.store.for_update(update['update_id']):
                return
            identifier = uuid.uuid4().hex[:16]
            self.registry.snapshot(plugin, identifier)
            job = {'id': identifier, 'update_id': update['update_id'], 'plugin': plugin.identifier, 'revision': plugin.revision,
                   'command': command, 'argument': argument, 'mode': spec['mode'], 'permission': spec['permission'],
                   'user_id': user['id'], 'chat_id': chat['id'], 'message_id': source,
                   'spec': {**spec, 'config': plugin.config, 'entrypoint': plugin.manifest.get('entrypoint','main.py'),
                            'timeout_seconds': spec.get('timeout_seconds', 30 if spec['mode']=='query' else 3600)}}
            self.store.add_job(job)
            self.reply('job:' + identifier + ':queued', '⏳ 已接受任务 ' + identifier + '\n/' + command, source, chat_id=chat['id'])

    def complete_notice(self, job):
        if not job['result']:
            return
        value = json.loads(job['result'])
        prefix = '✅' if value['ok'] else ('⚠️' if value['status'] in ('uncertain','push_pending') else '❌')
        self.reply('job:' + job['id'] + ':final', f"{prefix} 任务 {job['id']}\n" + value['message'], job['message_id'], chat_id=job['chat_id'])

    def work(self, mode):
        while not self.stop.is_set():
            job = self.store.claim(mode)
            if not job:
                self.stop.wait(0.5); continue
            try:
                if not permitted_chat(self.config, job['chat_id'], job['user_id']) or not permitted(self.config, job['user_id'], job['permission']):
                    raise RuntimeError('任务开始前授权已撤销，已停止执行。')
                def progress(text):
                    self.store.progress(job['id'], redact(text, self.api.token))
                value = execute(self.layout, job, self.stop, progress)
                value['message'] = redact(value['message'], self.api.token)
            except Exception as error:
                value = {'ok': False, 'status': 'uncertain' if self.stop.is_set() else 'failed', 'message': redact(error, self.api.token)[:1000], 'data': {}}
                self.layout.log(traceback.format_exc(), self.api.token)
            self.store.finish(job['id'], value)
            self.complete_notice(self.store.job(job['id']))

    def deliver_one(self):
        if self.store.get('telegram_retry_at', 0) > time.time():
            return False
        value = self.store.take_notification()
        if not value:
            return False
        if not permitted_chat(self.config, value['chat_id'], value['chat_id'] if value['chat_id'] > 0 else None):
            self.store.delivered(value['id'], 'failed', error='聊天入口已失效或授权已撤销'); return True
        try:
            sent = self.api.send(value['chat_id'], value['text'], value['message_id'])
            self.store.delivered(value['id'], 'sent', telegram_id=sent['message_id'])
        except ApiError as error:
            if error.code == 429:
                due = time.time()+int(error.parameters.get('retry_after',30))
                self.store.set('telegram_retry_at', due)
                self.store.delivered(value['id'], 'retry', due=due, error=str(error))
            else:
                self.store.delivered(value['id'], 'failed', error=redact(error, self.api.token))
        except Exception as error:
            self.store.delivered(value['id'], 'unknown', error=redact(error, self.api.token))
        return True

    def send_loop(self):
        while not self.stop.is_set():
            if not self.deliver_one():
                self.stop.wait(0.5)

    def sync_menus(self):
        with self.registry_lock:
            self.registry.discover()
            config = self.config
            # Snapshot menus while plugin discovery is locked. Networking happens
            # after releasing it so command dispatch stays available.
            menus = {user_id: commands_for(config, self.registry, user_id)
                     for user_id in {None, *config['allowed_user_ids'], config['chat_id']}}
        self.menu_publisher.sync(config, menus)

    def watch_loop(self):
        while not self.stop.is_set():
            self.menu_refresh.clear()
            try:
                self.sync_menus()
            except Exception as error:
                self.layout.log('Plugin/menu refresh: ' + str(error), self.api.token)
            self.menu_refresh.wait(5)

    def start_workers(self):
        self.store.recover()
        for job in self.store.jobs(1000):
            self.complete_notice(job)
        for function, name in ((lambda:self.work('job'),'job-worker'), (lambda:self.work('query'),'query-worker'), (self.send_loop,'telegram-outbox'), (self.watch_loop,'plugin-watch'), (self.health_loop,'health-watch')):
            thread = threading.Thread(target=function, name=name, daemon=True)
            self.workers.append(thread); thread.start()

    def run(self):
        with FileLock(self.layout.runtime / 'host.lock'), BotLock(self.config['bot_id']):
            atomic_json(self.layout.runtime / 'health.json',self.health_snapshot('starting'))
            me = self.api.call('getMe')
            if me['id'] != self.config['bot_id']:
                raise RuntimeError('Token 与已绑定机器人不一致，请重新 setup。')
            if self.api.call('getWebhookInfo').get('url'):
                raise RuntimeError('已配置 Webhook，未启动轮询。')
            if not self.store.get('offset', 0):
                self.store.set('offset', self.config.get('initial_offset',0))
            self.start_workers()
            failures = 0
            self.layout.log('Host started; bot=' + str(self.config['bot_id']))
            try:
                while not self.stop.is_set():
                    try:
                        for update in self.store.incoming():
                            try:
                                self.handle(update)
                            except Exception as error:
                                self.layout.log('Command rejected: ' + str(error), self.api.token)
                                message = update.get('message') or {}
                                sender = message.get('from') or {}
                                if permitted_message(self.config, message) and permitted(self.config,sender.get('id')):
                                    self.reply('update:'+str(update['update_id']), '命令受理失败：'+redact(error,self.api.token),message.get('message_id'),chat_id=message['chat']['id'])
                            self.store.handled(update['update_id'])
                        updates = self.api.call('getUpdates', {'offset':self.store.get('offset',0), 'timeout':20, 'limit':50, 'allowed_updates':['message']}, timeout=30)
                        for update in updates or []:
                            self.store.receive(update)
                        self.poll_result()
                        failures = 0
                    except ApiError as error:
                        self.poll_result(error)
                        if error.code in (401,409):
                            raise RuntimeError('Bot Token 已失效或被其他轮询服务占用，请检查独立机器人配置。') from None
                        failures += 1
                        self.layout.log(error, self.api.token)
                        self.stop.wait(min(30, 2 ** min(failures,5)))
                    except Exception as error:
                        self.poll_result(error)
                        failures += 1
                        self.layout.log(error, self.api.token)
                        self.stop.wait(min(30, 2 ** min(failures,5)))
            finally:
                self.stop.set()
                self.menu_refresh.set()
                for thread in self.workers:
                    thread.join(timeout=45)
                atomic_json(self.layout.runtime / 'health.json',self.health_snapshot('stopped'))
                self.store.close()
                self.layout.log('Host stopped')
        return 0
