from __future__ import annotations
import json
import io
from contextlib import nullcontext, redirect_stdout
import os
from pathlib import Path
import plistlib
import shutil
import sys
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
import xml.etree.ElementTree as ET

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'src'))
from telegram_task_runner.binding import binding_candidate, parse_command
from telegram_task_runner.executor import execute
from telegram_task_runner.host import Host
from telegram_task_runner.plugins import Registry
from telegram_task_runner.security import store_token, load_token
from telegram_task_runner.services import windows_xml, arguments, macos_definition, linux_definition, identity
from telegram_task_runner.storage import Store
from telegram_task_runner.telegram import ApiError, DeliveryUnknown
from telegram_task_runner.util import Layout, atomic_json, redact
from telegram_task_runner.health import read as read_health


class FakeApi:
    token = '123456:synthetic_token_for_tests_only'

    def __init__(self):
        self.sent = []
        self.error = None
        self.command_calls = []
        self.menus = {}

    def call(self, method, payload=None, **options):
        if method not in ('setMyCommands', 'deleteMyCommands'):
            raise AssertionError('Unexpected fake API method: ' + method)
        self.command_calls.append((method, payload))
        key = json.dumps(payload['scope'], sort_keys=True)
        if method == 'setMyCommands':
            self.menus[key] = payload['commands']
        else:
            self.menus.pop(key, None)
        return True

    def send(self, chat, text, reply=None):
        if self.error:
            raise self.error
        self.sent.append((chat,text,reply))
        return {'message_id':len(self.sent)}


class Sandbox(unittest.TestCase):
    def setUp(self):
        self.boundary = PROJECT / 'runtime' / 'test-workspaces'
        self.boundary.mkdir(parents=True, exist_ok=True)
        self.root = Path(tempfile.mkdtemp(prefix='runner-', dir=self.boundary)).resolve()
        self.layout = Layout(self.root).ensure()
        # Real worker subprocesses use the production package with an isolated application root.
        (self.root / 'runner.py').write_text('import sys\nsys.path.insert(0,' + repr(str(PROJECT / 'src')) + ')\nfrom telegram_task_runner.cli import main\nraise SystemExit(main())\n',encoding='utf-8')
        self.config = {'bot_id':123456,'bot_username':'TestRunnerBot','chat_id':-100,
                       'allowed_user_ids':[1,2],'admin_user_ids':[1],'command_max_age_seconds':900}
        self.api = FakeApi()
        self.host = None

    def tearDown(self):
        if self.host:
            self.host.store.close()
        target = self.root.resolve()
        if target.parent != self.boundary.resolve() or not target.name.startswith('runner-'):
            raise RuntimeError('Refusing cleanup outside verified disposable test root.')
        shutil.rmtree(target)

    def plugin(self, ident='demo', code=None, commands=None, **options):
        directory = self.layout.plugins / ident
        directory.mkdir(exist_ok=True)
        manifest = {'api_version':1,'id':ident,'version':'1.0.0','entrypoint':'main.py','files':['main.py'],
                    'commands':commands or {ident:{'description':'测试命令','mode':'job','permission':'execute','timeout_seconds':5}}, **options}
        atomic_json(directory / 'plugin.json', manifest)
        (directory / 'main.py').write_text(code or "from telegram_task_runner.plugin_api import result\ndef handle(request,ctx): return result('original',data={'actor':request['user_id']})\n",encoding='utf-8')
        return directory

    def make_host(self):
        self.host = Host(self.layout, self.config, self.api)
        return self.host

    def update(self, text='/demo@TestRunnerBot', user=1, chat=-100, identifier=10, **extra):
        return {'update_id':identifier,'message':{'message_id':identifier,'date':int(time.time()),'chat':{'id':chat,'type':'supergroup'},'from':{'id':user},'text':text,**extra}}

    def private_update(self, text='/demo', user=1, identifier=10, **extra):
        update = self.update(text,user=user,chat=user,identifier=identifier,**extra)
        update['message']['chat']['type'] = 'private'
        return update


class RegistryTests(Sandbox):
    def test_private_git_folder_is_not_loaded_or_snapshotted(self):
        folder = self.plugin()
        (folder / '.git').mkdir(); (folder / '.git' / 'private').write_text('private metadata')
        registry = Registry(self.layout).discover()
        self.assertEqual(set(registry.plugins['demo'].files), {'main.py','plugin.json'})
        registry.snapshot(registry.plugins['demo'],'snapshot')
        self.assertFalse((self.layout.runtime/'jobs/snapshot/code/.git').exists())

    def test_dynamic_discovery_update_disable_remove(self):
        registry = Registry(self.layout).discover()
        self.assertFalse(registry.commands)
        folder = self.plugin()
        first = registry.discover().plugins['demo'].revision
        (folder/'main.py').write_text("def handle(request,ctx): return {'new':True}\n",encoding='utf-8')
        self.assertNotEqual(first, registry.discover().plugins['demo'].revision)
        value = json.loads((folder/'plugin.json').read_text(encoding='utf-8'))
        value['enabled'] = False; atomic_json(folder/'plugin.json',value)
        self.assertFalse(registry.discover().commands)
        (folder/'plugin.json').unlink()
        self.assertFalse(registry.discover().commands)

    def test_invalid_and_conflicting_plugins_do_not_disable_other_plugins(self):
        shared = {'collision':{'description':'test','mode':'query','permission':'view'}}
        self.plugin('first', commands=shared); self.plugin('second', commands=shared)
        self.plugin('healthy')
        folder = self.plugin('broken'); (folder/'main.py').write_text('def ???',encoding='utf-8')
        registry = Registry(self.layout).discover()
        self.assertEqual(set(registry.plugins), {'healthy'})
        self.assertEqual(set(registry.errors), {'first','second','broken'})

    def test_platform_declaration_disables_only_unsupported_plugin(self):
        self.plugin('winonly', platforms=['windows']); self.plugin('portable')
        registry = Registry(self.layout, platform='macos').discover()
        self.assertEqual(set(registry.plugins), {'portable'})
        self.assertIn('winonly',registry.errors)
        self.assertIn('winonly',Registry(self.layout,platform='windows').discover().plugins)

    def test_traversal_and_reserved_commands_rejected(self):
        self.plugin('escape',files=['main.py','../outside.py'])
        self.plugin('reserved', commands={'help':{'description':'x','mode':'query','permission':'view'}})
        registry = Registry(self.layout).discover()
        self.assertEqual(set(registry.errors), {'escape','reserved'})

    def test_config_revision_and_worker_snapshot_are_fixed(self):
        self.plugin()
        registry = Registry(self.layout).discover()
        before = registry.plugins['demo'].revision
        atomic_json(self.layout.runtime/'plugin-state/demo/config.json',{'new':True})
        self.assertNotEqual(before, registry.discover().plugins['demo'].revision)
        self.assertEqual(registry.plugins['demo'].config,{'new':True})


class HostTests(Sandbox):
    def test_health_reports_stale_heartbeat_instead_of_claiming_process_is_online(self):
        value = {'started_at':1,'heartbeat_at':10,'lifecycle':'running','workers':{'job-worker':True},
                 'telegram':{'last_poll_success_at':10,'consecutive_failures':0}}
        atomic_json(self.layout.runtime/'health.json',value)
        self.assertEqual(read_health(self.layout,now=12)['state'],'healthy')
        self.assertEqual(read_health(self.layout,now=40)['state'],'stale')

    def test_health_recovers_after_connection_failure_and_redacts_token(self):
        host=self.make_host()
        host.poll_result(RuntimeError('failure for ' + self.api.token))
        value=host.health_snapshot()
        self.assertEqual(value['telegram']['state'],'retrying')
        self.assertNotIn(self.api.token,json.dumps(value))
        host.poll_result()
        self.assertEqual(host.health_snapshot()['telegram']['state'],'online')
        self.assertEqual(host.health_snapshot()['telegram']['consecutive_failures'],0)

    def test_chat_user_anonymous_and_bot_authorization_precede_plugin_dispatch(self):
        self.plugin(); host = self.make_host()
        for update in (self.update(chat=-999),self.update(user=99),self.update(sender_chat={'id':-100}),
                       self.update(**{'from':{'id':1,'is_bot':True}})):
            host.handle(update)
        self.assertFalse(host.store.jobs())
        self.assertFalse(list((self.layout.runtime/'jobs').iterdir()))

    def test_command_target_and_edited_messages_do_not_execute(self):
        self.plugin(); host = self.make_host()
        host.handle(self.update('/demo@DifferentBot'))
        update = self.update(); host.handle({'update_id':12,'edited_message':update['message']})
        self.assertFalse(host.store.jobs())
        self.assertEqual(parse_command('/demo@TestRunnerBot a b','testrunnerbot'),('demo','a b'))

    def test_duplicate_update_runs_side_effect_once_and_uses_old_snapshot(self):
        folder = self.plugin(); host = self.make_host()
        update = self.update()
        host.handle(update); host.handle(update)
        self.assertEqual(len(host.store.jobs()),1)
        self.plugin(code="from telegram_task_runner.plugin_api import result\ndef handle(request,ctx): return result('updated')\n")
        job = host.store.claim('job')
        value = execute(self.layout,job)
        self.assertEqual(value['message'],'original')
        self.assertEqual(value['data']['actor'],1)
        host.store.finish(job['id'],value)
        host.handle(self.update(identifier=11))
        second = host.store.claim('job')
        self.assertEqual(execute(self.layout,second)['message'],'updated')

    def test_admin_only_command_and_self_id_onboarding(self):
        host = self.make_host()
        host.handle(self.update('/allow 99',user=2))
        self.assertNotIn(99,host.config['allowed_user_ids'])
        host.handle(self.update('/whoami',user=99,identifier=11))
        self.assertTrue(host.deliver_one()); self.assertTrue(host.deliver_one())
        self.assertTrue(any('99' in text for _,text,_ in self.api.sent))
        host.handle(self.update('/allow 99',identifier=12))
        self.assertIn(99,host.config['allowed_user_ids'])

    def test_stale_command_is_rejected(self):
        self.plugin(); host = self.make_host()
        host.handle(self.update(date=1))
        self.assertFalse(host.store.jobs())

    def test_resource_claims_serialize_and_queries_remain_available(self):
        specs = {
            'write':{'description':'write','mode':'job','permission':'execute','resources':['repo']},
            'read':{'description':'read','mode':'query','permission':'view'}}
        self.plugin(commands=specs); host = self.make_host()
        host.handle(self.update('/write',identifier=1)); host.handle(self.update('/write',identifier=2)); host.handle(self.update('/read',identifier=3))
        first = host.store.claim('job')
        self.assertIsNotNone(first)
        self.assertIsNone(host.store.claim('job'))
        self.assertIsNotNone(host.store.claim('query'))
        host.store.finish(first['id'],{'status':'succeeded','ok':True,'message':'done','data':{}})
        self.assertIsNotNone(host.store.claim('job'))

    def test_notification_failure_never_requeues_finished_job(self):
        self.plugin(); host = self.make_host(); host.handle(self.update())
        job = host.store.claim('job')
        host.store.finish(job['id'],{'status':'succeeded','ok':True,'message':'published','data':{}})
        host.complete_notice(host.store.job(job['id']))
        self.api.error = DeliveryUnknown('ambiguous transport')
        while host.deliver_one():
            pass
        self.assertEqual(host.store.job(job['id'])['status'],'succeeded')
        self.assertIsNone(host.store.claim('job'))
        rows = host.store.db.execute('SELECT status FROM outbox').fetchall()
        self.assertTrue(all(row[0]=='unknown' for row in rows))

    def test_retry_after_is_persisted_for_notifications_only(self):
        host = self.make_host(); host.reply('one','notification')
        self.api.error = ApiError(429,'slow down',{'retry_after':10})
        self.assertTrue(host.deliver_one()); self.assertFalse(host.deliver_one())
        self.assertGreater(host.store.get('telegram_retry_at'),time.time())

    def test_crash_recovery_marks_running_uncertain_and_preserves_queued(self):
        self.plugin(); host = self.make_host()
        host.handle(self.update(identifier=1)); host.handle(self.update(identifier=2))
        active = host.store.claim('job')
        self.assertEqual(host.store.recover(),1)
        self.assertEqual(host.store.job(active['id'])['status'],'uncertain')
        next_job = host.store.claim('job')
        self.assertNotEqual(active['id'],next_job['id'])

    def test_inbox_offset_and_payload_survive_reopen(self):
        store = Store(self.layout.runtime/'inbox-test.sqlite3')
        store.receive(self.update(identifier=20)); store.close()
        store = Store(self.layout.runtime/'inbox-test.sqlite3')
        self.assertEqual(store.get('offset'),21)
        self.assertEqual(store.incoming()[0]['update_id'],20)
        store.receive(self.update(identifier=20))
        self.assertEqual(len(store.incoming()),1)
        store.close()

    def test_worker_timeout_kills_its_process(self):
        self.plugin(code='import time\ndef handle(request,ctx): time.sleep(30)\n',commands={'demo':{'description':'x','mode':'job','permission':'execute','timeout_seconds':1}})
        host = self.make_host(); host.handle(self.update())
        job = host.store.claim('job')
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            execute(self.layout,job)
        self.assertLess(time.monotonic()-start,8)

    def test_worker_does_not_forward_parent_secret_environment(self):
        self.plugin(code="import os\nfrom telegram_task_runner.plugin_api import result\ndef handle(request,ctx): return result(str(os.environ.get('PRIVATE_TEST_SECRET')))\n")
        host = self.make_host(); host.handle(self.update())
        job = host.store.claim('job')
        with patch.dict(os.environ,PRIVATE_TEST_SECRET='never_forward_this'):
            self.assertEqual(execute(self.layout,job)['message'],'None')

    def test_plugin_exception_returns_failure_without_killing_host(self):
        self.plugin(code="def handle(request,ctx): raise RuntimeError('intentional plugin failure')\n")
        host = self.make_host(); host.handle(self.update())
        job = host.store.claim('job')
        value = execute(self.layout,job)
        self.assertFalse(value['ok'])
        self.assertEqual(value['status'],'failed')
        self.assertIn('intentional plugin failure',value['message'])
        host.store.finish(job['id'],value)
        self.assertIsNotNone(host.store.job(job['id']))

    def test_full_poll_dispatch_worker_and_notification_flow(self):
        self.plugin(); host = self.make_host()
        state = {'received':False}
        def call(method,payload=None,**options):
            if method == 'getMe':
                return {'id':123456,'username':'TestRunnerBot'}
            if method == 'getWebhookInfo':
                return {'url':''}
            if method == 'setMyCommands':
                return True
            if not state['received']:
                state['received'] = True
                return [self.update()]
            host.stop.wait(0.02)
            return []
        self.api.call = call
        original = self.api.send
        def send(chat,text,reply):
            value = original(chat,text,reply)
            if 'original' in text:
                host.stop.set()
            return value
        self.api.send = send
        # This tests the actual controller loop and real worker subprocess, not Telegram networking.
        import threading
        watchdog = threading.Timer(10,host.stop.set)
        watchdog.start()
        try:
            self.assertEqual(host.run(),0)
        finally:
            watchdog.cancel()
        self.assertTrue(any('original' in text for _,text,_ in self.api.sent))


class PrivateChatTests(Sandbox):
    def test_admin_builtin_commands_reply_to_their_origin_in_both_chats(self):
        host = self.make_host()
        commands = ('/start','/help','/whoami','/status','/plugins','/jobs','/job missing',
                    '/users','/allow 3','/allow invalid','/revoke 3','/revoke 1','/unknown')
        identifier = 100
        for command in commands:
            for private in (False,True):
                update = (self.private_update if private else self.update)(command,identifier=identifier)
                update['message']['message_id'] = 7
                host.handle(update)
                self.assertTrue(host.deliver_one())
                self.assertEqual(self.api.sent[-1][0],1 if private else -100)
                self.assertEqual(self.api.sent[-1][2],7)
                identifier += 1
        self.assertEqual(len(self.api.sent),len(commands)*2)
        self.assertEqual(host.config['chat_id'],-100)

    def test_other_private_users_chats_and_invalid_senders_are_rejected(self):
        self.plugin(); host = self.make_host()
        forged = self.private_update(); forged['message']['chat']['id'] = 2
        wrong_type = self.private_update(); wrong_type['message']['chat']['type'] = 'channel'
        updates = (self.private_update(user=2),self.private_update(user=99),self.update(chat=-999),
                   self.update(chat=1),forged,wrong_type,
                   self.private_update(**{'from':{'id':1,'is_bot':True}}),
                   self.private_update(sender_chat={'id':-100}))
        for update in updates:
            host.handle(update)
        self.assertFalse(host.store.jobs())
        self.assertFalse(host.store.notification_counts())
        self.assertFalse(host.store.get('admin_private_chats',[]))

    def test_chats_share_resources_and_query_only_their_own_jobs(self):
        self.plugin(commands={'demo':{'description':'demo','mode':'job','permission':'execute','resources':['shared_workspace']}})
        host = self.make_host(); host.handle(self.private_update())
        private_job = host.store.jobs(chat_id=1)[0]
        for identifier in range(11,23):
            host.handle(self.update(user=2,identifier=identifier))
        group_jobs = host.store.jobs(100,chat_id=-100)
        self.assertEqual(len(group_jobs),12)
        self.assertEqual(len(host.store.jobs(chat_id=1)),1)
        self.assertEqual(host.store.claim('job')['id'],private_job['id'])
        self.assertIsNone(host.store.claim('job'))
        host.store.finish(private_job['id'],{'status':'succeeded','ok':True,'message':'private result','data':{}})
        self.assertEqual(host.store.claim('job')['chat_id'],-100)
        while host.deliver_one():
            pass
        host.handle(self.private_update('/jobs',identifier=30)); host.deliver_one()
        text = self.api.sent[-1][1]
        self.assertIn(private_job['id'],text)
        self.assertTrue(all(job['id'] not in text for job in group_jobs))
        host.handle(self.update('/jobs',identifier=31)); host.deliver_one()
        self.assertNotIn(private_job['id'],self.api.sent[-1][1])
        host.handle(self.update('/job '+private_job['id'],identifier=32)); host.deliver_one()
        self.assertNotIn('private result',self.api.sent[-1][1])
        self.assertNotIn(private_job['id'],self.api.sent[-1][1])
        host.handle(self.private_update('/job '+group_jobs[0]['id'],identifier=33)); host.deliver_one()
        self.assertNotIn(group_jobs[0]['id'],self.api.sent[-1][1])
        host.handle(self.private_update('/job '+private_job['id'],identifier=34)); host.deliver_one()
        self.assertIn('private result',self.api.sent[-1][1])

    def test_private_job_rechecks_admin_before_execution_and_delivery(self):
        self.plugin(); host = self.make_host(); host.handle(self.private_update())
        job = host.store.jobs()[0]
        host.config = {**host.config,'admin_user_ids':[2]}
        original = host.store.claim
        def claim(mode):
            value = original(mode)
            if value is None:
                host.stop.set()
            return value
        with patch.object(host.store,'claim',side_effect=claim), patch('telegram_task_runner.host.execute') as execute_mock:
            host.work('job')
        execute_mock.assert_not_called()
        self.assertEqual(host.store.job(job['id'])['status'],'failed')
        while host.deliver_one():
            pass
        self.assertFalse(self.api.sent)
        self.assertEqual(host.store.notification_counts(),{'failed':2})

    def test_private_job_result_and_notice_survive_restart_without_duplication(self):
        self.plugin(); host = self.make_host(); host.handle(self.private_update())
        job = host.store.claim('job')
        host.store.finish(job['id'],execute(self.layout,job))
        host.complete_notice(host.store.job(job['id']))
        while host.deliver_one():
            pass
        self.assertEqual(len(self.api.sent),2)
        self.assertTrue(all(chat == 1 for chat,_,_ in self.api.sent))
        host.store.close()
        self.host = Host(self.layout,host.config,self.api)
        self.assertEqual(self.host.store.recover(),0)
        self.host.complete_notice(self.host.store.job(job['id']))
        self.assertFalse(self.host.deliver_one())
        self.assertEqual(len(self.api.sent),2)
        self.assertEqual(self.host.store.job(job['id'])['status'],'succeeded')

    def test_full_poll_routes_private_and_group_jobs_and_handler_errors(self):
        self.plugin(code="from telegram_task_runner.plugin_api import result\ndef handle(request,ctx): return result('done '+str(request['chat_id']))\n")
        host = self.make_host()
        updates = [self.private_update('/broken',identifier=100),self.private_update(identifier=101),self.update(identifier=102)]
        for update in updates[1:]:
            update['message']['message_id'] = 7
        state = {'received':False}
        original_call, original_handle, original_send = self.api.call,host.handle,self.api.send
        def call(method,payload=None,**options):
            if method == 'getMe':
                return {'id':123456,'username':'TestRunnerBot'}
            if method == 'getWebhookInfo':
                return {'url':''}
            if method in ('setMyCommands','deleteMyCommands'):
                return original_call(method,payload,**options)
            self.assertEqual(method,'getUpdates')
            if not state['received']:
                state['received'] = True
                return updates
            host.stop.wait(0.02)
            return []
        def handle(update):
            if update['message']['text'] == '/broken':
                raise ValueError('simulated handler failure')
            return original_handle(update)
        def send(chat,text,reply):
            value = original_send(chat,text,reply)
            if sum('done ' in message for _,message,_ in self.api.sent) == 2:
                host.stop.set()
            return value
        self.api.call,self.api.send,host.handle = call,send,handle
        import threading
        watchdog = threading.Timer(10,host.stop.set); watchdog.start()
        try:
            self.assertEqual(host.run(),0)
        finally:
            watchdog.cancel()
        finals = {(chat,text.splitlines()[-1],reply) for chat,text,reply in self.api.sent if 'done ' in text}
        self.assertEqual(finals,{(1,'done 1',7),(-100,'done -100',7)})
        errors = [(chat,text) for chat,text,_ in self.api.sent if 'simulated handler failure' in text]
        self.assertEqual(len(errors),1)
        self.assertEqual(errors[0][0],1)


class MenuTests(Sandbox):
    def names(self, kind='chat', user=None, chat=-100):
        scope = {'type': kind, 'chat_id': chat}
        if user is not None:
            scope['user_id'] = user
        return {item['command'] for item in self.api.menus.get(json.dumps(scope, sort_keys=True), [])}

    def test_menu_help_plugins_and_execution_use_the_same_roles(self):
        self.plugin(commands={
            'inspect': {'description':'inspect', 'mode':'query', 'permission':'view'},
            'build': {'description':'build', 'mode':'job', 'permission':'execute'},
            'secret': {'description':'secret', 'mode':'job', 'permission':'admin'}})
        host = self.make_host()
        self.assertEqual({c['command'] for c in host.menu(99)}, {'help','whoami'})
        regular = {c['command'] for c in host.menu(2)}
        self.assertTrue({'inspect','build','status'} <= regular)
        self.assertTrue({'secret','allow','revoke','users'}.isdisjoint(regular))
        self.assertTrue({'secret','allow','revoke','users'} <= {c['command'] for c in host.menu(1)})
        for index,user in enumerate((99,2,1), 20):
            host.handle(self.update('/help',user=user,identifier=index))
            host.deliver_one()
            text = self.api.sent[-1][1]
            self.assertEqual('/secret —' in text, user == 1)
            self.assertEqual('/inspect —' in text, user != 99)
        for index,user in enumerate((2,1), 30):
            host.handle(self.update('/plugins',user=user,identifier=index))
            host.deliver_one()
            self.assertEqual('/secret' in self.api.sent[-1][1], user == 1)
        host.handle(self.update('/secret',user=2,identifier=40))
        self.assertFalse(host.store.jobs())

    def test_group_scopes_separate_app_roles_without_inheriting_telegram_admin(self):
        self.plugin(); host = self.make_host()
        host.sync_menus()
        self.assertEqual(self.names(), {'help','whoami'})
        self.assertEqual(self.names('chat_administrators'), {'help','whoami'})
        self.assertIn('allow', self.names('chat_member',1))
        self.assertNotIn('allow', self.names('chat_member',2))
        self.assertIn('demo', self.names('chat_member',2))
        count = len(self.api.command_calls)
        host.sync_menus()
        self.assertEqual(len(self.api.command_calls),count)
        host.handle(self.update('/allow 99'))
        self.assertTrue(host.menu_refresh.is_set())
        host.sync_menus()
        self.assertIn('demo',self.names('chat_member',99))

    def test_revocation_cleans_persisted_member_scope_after_restart(self):
        self.plugin(); host = self.make_host(); host.sync_menus()
        host.handle(self.update('/revoke 2'))
        host.handle(self.update('/demo',user=2,identifier=11))
        self.assertFalse(host.store.jobs())
        host.store.close()
        self.host = Host(self.layout,host.config,self.api)
        self.host.sync_menus()
        self.assertEqual(self.host.store.get('command_menu_members'),[1])
        self.assertFalse(self.names('chat_member',2))
        self.assertEqual(self.names(),{'help','whoami'})
        self.assertTrue(any(method == 'deleteMyCommands' and payload['scope']['user_id'] == 2
                            for method,payload in self.api.command_calls))

    def test_plugin_hot_load_permission_change_removal_and_admin_demotion_refresh_menus(self):
        host = self.make_host(); host.sync_menus()
        folder = self.plugin(); host.sync_menus()
        self.assertIn('demo',self.names('chat_member',2))
        manifest = json.loads((folder/'plugin.json').read_text(encoding='utf-8'))
        manifest['commands']['demo']['permission'] = 'admin'
        atomic_json(folder/'plugin.json',manifest); host.sync_menus()
        self.assertNotIn('demo',self.names('chat_member',2))
        self.assertIn('demo',self.names('chat_member',1))
        host.config = {**host.config,'admin_user_ids':[2]}
        host.sync_menus()
        self.assertNotIn('allow',self.names('chat_member',1))
        self.assertNotIn('demo',self.names('chat_member',1))
        self.assertIn('demo',self.names('chat_member',2))
        manifest['enabled'] = False
        atomic_json(folder/'plugin.json',manifest); host.sync_menus()
        self.assertNotIn('demo',self.names('chat_member',2))

    def test_ambiguous_scope_update_is_tracked_and_other_members_still_sync(self):
        host = self.make_host()
        original = self.api.call
        def call(method,payload=None,**options):
            value = original(method,payload,**options)
            if payload['scope'].get('user_id') == 1:
                raise DeliveryUnknown('response lost after setting member commands')
            return value
        with patch.object(self.api,'call',side_effect=call):
            with self.assertRaises(RuntimeError):
                host.sync_menus()
        self.assertIn('allow',self.names('chat_member',1))
        self.assertIn('status',self.names('chat_member',2))
        self.assertEqual(host.store.get('command_menu_members'),[1,2])
        host.store.close()
        self.host = Host(self.layout,{**host.config,'allowed_user_ids':[2],'admin_user_ids':[2]},self.api)
        self.host.sync_menus()
        self.assertFalse(self.names('chat_member',1))

    def test_failed_revocation_retains_cleanup_intent_and_respects_retry_after(self):
        host = self.make_host(); host.sync_menus()
        host.handle(self.update('/revoke 2'))
        original = self.api.call
        def call(method,payload=None,**options):
            if method == 'deleteMyCommands':
                raise ApiError(429,'slow down',{'retry_after':10})
            return original(method,payload,**options)
        with patch('telegram_task_runner.menus.time.time',return_value=1000), patch.object(self.api,'call',side_effect=call):
            with self.assertRaises(RuntimeError):
                host.sync_menus()
        self.assertIn(2,host.store.get('command_menu_members'))
        count = len(self.api.command_calls)
        with patch('telegram_task_runner.menus.time.time',return_value=1005):
            host.sync_menus()
        self.assertEqual(len(self.api.command_calls),count)
        with patch('telegram_task_runner.menus.time.time',return_value=1011):
            host.sync_menus()
        self.assertNotIn(2,host.store.get('command_menu_members'))
        self.assertFalse(self.names('chat_member',2))

    def test_private_binding_uses_chat_scope_for_actual_user_only(self):
        self.plugin(); self.config['chat_id'] = 2
        host = self.make_host(); host.sync_menus()
        self.assertIn('demo',self.names(chat=2))
        self.assertNotIn('allow',self.names(chat=2))
        self.assertTrue(all(payload['scope']['type'] == 'chat' for _,payload in self.api.command_calls))

    def test_admin_private_menu_is_registered_after_contact_and_hot_loads_plugins(self):
        self.plugin(); host = self.make_host(); host.sync_menus()
        self.assertFalse(self.names(chat=1))
        host.handle(self.private_update('/start',user=2))
        host.sync_menus()
        self.assertFalse(self.names(chat=2))
        host.handle(self.private_update('/start',identifier=11))
        host.sync_menus()
        self.assertTrue({'demo','allow','users'} <= self.names(chat=1))
        self.assertEqual(self.names(),{'help','whoami'})
        self.assertEqual(host.store.get('command_menu_private_chats'),[1])
        self.plugin('added'); host.sync_menus()
        self.assertIn('added',self.names(chat=1))
        self.assertIn('added',self.names('chat_member',1))

    def test_revoked_admin_private_menu_is_cleaned_after_restart(self):
        self.plugin(); host = self.make_host()
        host.handle(self.private_update('/start')); host.sync_menus()
        self.assertIn('allow',self.names(chat=1))
        host.config = {**host.config,'admin_user_ids':[2]}
        host.store.close()
        self.host = Host(self.layout,host.config,self.api)
        self.host.sync_menus()
        self.assertFalse(self.names(chat=1))
        self.assertEqual(self.host.store.get('command_menu_private_chats'),[])
        self.assertNotIn('allow',self.names('chat_member',1))
        self.host.handle(self.private_update('/start',user=2,identifier=11))
        self.host.sync_menus()
        self.assertIn('allow',self.names(chat=2))
        self.assertFalse(self.names(chat=1))


class PlatformTests(Sandbox):
    def test_windows_login_startup_never_requires_password_or_boot_trigger(self):
        ns = {'t':'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        argv = [r'C:\some space\pythonw.exe',r'D:\repo space\runner.py','--root',r'D:\repo space','run']
        login = ET.fromstring(windows_xml(argv,self.root,'machine\\developer'))
        self.assertIsNone(login.find('t:Triggers/t:BootTrigger',ns))
        self.assertEqual(login.findtext('t:Principals/t:Principal/t:LogonType',namespaces=ns),'InteractiveToken')
        self.assertEqual(login.findtext('t:Triggers/t:LogonTrigger/t:UserId',namespaces=ns),'machine\\developer')
        with self.assertRaises(ValueError):
            windows_xml(argv,self.root,'machine\\developer','startup')

    def test_macos_plist_uses_exact_argument_array(self):
        argv = ['/path with space/python','/repo/runner.py','--root','/repo','run']
        data = plistlib.loads(plistlib.dumps(macos_definition(self.layout,argv)))
        self.assertEqual(data['ProgramArguments'],argv)
        self.assertTrue(data['RunAtLoad'])

    def test_linux_unit_escapes_specifiers_and_shell_characters(self):
        definition = linux_definition(self.layout,['/venv/bin/python','/a b/runner.py','--root','/100%/$HOME','run'])
        self.assertIn('"/a b/runner.py"',definition)
        self.assertIn('"/100%%/$$HOME"',definition)
        self.assertIn('KillMode=control-group',definition)

    def test_service_names_are_scoped_to_application_root(self):
        self.assertNotEqual(identity(self.root),identity(self.root/'different'))
        self.assertNotIn('Codex',identity(self.root))

    @unittest.skipUnless(os.name=='nt','Windows DPAPI test')
    def test_windows_dpapi_roundtrip_with_synthetic_secret(self):
        token = '123456:synthetic_not_a_real_bot_token'
        secret = store_token(token,self.root)
        self.assertNotIn(token,json.dumps(secret))
        self.assertEqual(load_token({'credentials':secret}),token)

    def test_posix_uses_explicit_secure_backend_and_no_plaintext_fallback(self):
        for platform in ('macos','linux'):
            backend = Mock(); backend.get_password.return_value = 'synthetic'
            with patch('telegram_task_runner.security.current',return_value=platform),patch('telegram_task_runner.security.keyring_backend',return_value=backend):
                value = store_token('synthetic',self.root)
                self.assertEqual(value['backend'],platform)
                self.assertEqual(load_token({'credentials':value}),'synthetic')
            with patch('telegram_task_runner.security.current',return_value=platform),patch('telegram_task_runner.security.keyring_backend',side_effect=RuntimeError('locked')):
                with self.assertRaises(RuntimeError):
                    store_token('synthetic',self.root)

    def test_binding_challenge_and_target_are_required(self):
        self.assertIsNotNone(binding_candidate(self.update('/bind@TestRunnerBot nonce'),'TestRunnerBot','nonce'))
        self.assertIsNone(binding_candidate(self.update('/bind@OtherBot nonce'),'TestRunnerBot','nonce'))
        self.assertIsNone(binding_candidate(self.update('/bind@TestRunnerBot stale'),'TestRunnerBot','nonce'))
        self.assertIsNone(binding_candidate(self.update('/bind@TestRunnerBot nonce',sender_chat={'id':-100}),'TestRunnerBot','nonce'))

    def test_setup_binds_confirmed_group_without_saving_plaintext_token(self):
        from telegram_task_runner.binding import setup, load_config
        api = FakeApi()
        def call(method,payload=None,**options):
            if method == 'getMe': return {'id':123456,'username':'TestRunnerBot'}
            if method == 'getWebhookInfo': return {'url':''}
            if method == 'getChat': return {'id':-100,'type':'supergroup'}
            return [self.update('/bind@TestRunnerBot nonce')]
        api.call = call
        with patch('telegram_task_runner.binding.Telegram',return_value=api), \
             patch('telegram_task_runner.binding.getpass.getpass',return_value=api.token), \
             patch('telegram_task_runner.binding.secrets.token_hex',return_value='nonce'), \
             patch('telegram_task_runner.binding.store_token',return_value={'backend':'test-reference','account':'local-reference'}), \
             patch('telegram_task_runner.binding.BotLock',return_value=nullcontext()), \
             patch('builtins.input',side_effect=['y','2, 3']), redirect_stdout(io.StringIO()):
            self.assertEqual(setup(self.layout),0)
        config = load_config(self.layout)
        self.assertEqual(config['chat_id'],-100)
        self.assertEqual(config['admin_user_ids'],[1])
        self.assertEqual(config['allowed_user_ids'],[1,2,3])
        self.assertNotIn(api.token,self.layout.config.read_text(encoding='utf-8'))

    def test_optional_allowlist_input_retries_without_reentering_token(self):
        from telegram_task_runner.binding import setup, load_config
        api = FakeApi()
        def call(method,payload=None,**options):
            if method == 'getMe': return {'id':123456,'username':'TestRunnerBot'}
            if method == 'getWebhookInfo': return {'url':''}
            if method == 'getChat': return {'id':-100,'type':'supergroup'}
            return [self.update('/bind@TestRunnerBot nonce')]
        api.call = call
        with patch('telegram_task_runner.binding.Telegram',return_value=api), \
             patch('telegram_task_runner.binding.getpass.getpass',return_value=api.token) as token_input, \
             patch('telegram_task_runner.binding.secrets.token_hex',return_value='nonce'), \
             patch('telegram_task_runner.binding.store_token',return_value={'backend':'test-reference','account':'local-reference'}), \
             patch('telegram_task_runner.binding.BotLock',return_value=nullcontext()), \
             patch('builtins.input',side_effect=['y','@colleague','']), redirect_stdout(io.StringIO()):
            self.assertEqual(setup(self.layout),0)
        token_input.assert_called_once()
        self.assertEqual(load_config(self.layout)['allowed_user_ids'],[1])

    def test_redaction_removes_raw_and_url_tokens(self):
        value = '123456:synthetic_token_for_tests_only'
        self.assertNotIn(value,redact('https://api.telegram.org/bot'+value+'/getMe'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
