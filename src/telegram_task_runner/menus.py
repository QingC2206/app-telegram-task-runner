"""Publish command suggestions using the same permissions as command execution."""
from __future__ import annotations
import json
import time
from .binding import permitted
from .telegram import ApiError


BUILTINS = (
    ('help', '查看可用命令', 'public'),
    ('status', '查看宿主状态', 'execute'),
    ('plugins', '查看插件状态', 'execute'),
    ('jobs', '查看最近任务', 'execute'),
    ('job', '查看指定任务', 'execute'),
    ('whoami', '查看自己的用户 ID', 'public'),
    ('users', '查看授权用户（管理员）', 'admin'),
    ('allow', '授权用户（管理员）', 'admin'),
    ('revoke', '撤销用户授权（管理员）', 'admin'),
)


def commands_for(config, registry, user_id=None):
    commands = [{'command': name, 'description': description}
                for name, description, permission in BUILTINS
                if permission == 'public' or permitted(config, user_id, permission)]
    commands.extend({'command': name, 'description': spec['description']}
                    for name, (_, spec) in sorted(registry.commands.items())
                    if permitted(config, user_id, spec['permission']))
    return commands[:100]


class MenuPublisher:
    def __init__(self, api, store):
        self.api, self.store = api, store
        self.signatures = {}
        self.retry_at = 0

    def sync(self, config, menus):
        if time.time() < self.retry_at:
            return
        chat_id = config['chat_id']
        public = menus[None]
        members = set(config['allowed_user_ids']) if chat_id < 0 else set()
        known = set(self.store.get('command_menu_members', []))
        if members - known:
            known.update(members)
            # Record intent before networking: even an ambiguous API response can
            # create a scope that must be removed after a later revocation.
            self.store.set('command_menu_members', sorted(known))
        actions = [('setMyCommands', {'type': 'chat', 'chat_id': chat_id},
                    public if chat_id < 0 else menus[chat_id])]
        if chat_id < 0:
            # Telegram group admins do not automatically become application admins.
            actions.append(('setMyCommands', {'type': 'chat_administrators', 'chat_id': chat_id}, public))
        actions.extend(('deleteMyCommands', {'type': 'chat_member', 'chat_id': chat_id, 'user_id': user_id}, None)
                       for user_id in sorted(known - members))
        actions.extend(('setMyCommands', {'type': 'chat_member', 'chat_id': chat_id, 'user_id': user_id},
                        menus[user_id]) for user_id in sorted(members))
        errors = []
        for method, scope, commands in actions:
            key = json.dumps(scope, sort_keys=True)
            signature = json.dumps(commands, sort_keys=True, ensure_ascii=False)
            if method == 'setMyCommands' and self.signatures.get(key) == signature:
                continue
            payload = {'scope': scope, 'language_code': ''}
            if commands is not None:
                payload['commands'] = commands
            try:
                self.api.call(method, payload)
            except Exception as error:
                errors.append(str(scope) + ': ' + str(error))
                if isinstance(error, ApiError) and error.code == 429:
                    self.retry_at = time.time() + max(1, int(error.parameters.get('retry_after', 30)))
                    break
                continue
            if method == 'deleteMyCommands':
                known.discard(scope['user_id'])
                self.store.set('command_menu_members', sorted(known))
                self.signatures.pop(key, None)
            else:
                self.signatures[key] = signature
        if errors:
            raise RuntimeError('; '.join(errors))
