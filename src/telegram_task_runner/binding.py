from __future__ import annotations
import getpass
import json
import re
import secrets
import time
from .locking import BotLock
from .security import store_token
from .telegram import Telegram
from .util import atomic_json


def load_config(layout):
    if not layout.config.is_file():
        raise RuntimeError('尚未绑定机器人，请先运行 setup。')
    config = json.loads(layout.config.read_text(encoding='utf-8'))
    for key in ('allowed_user_ids', 'admin_user_ids'):
        if not isinstance(config.get(key), list) or not config[key] or any(type(v) is not int or v <= 0 for v in config[key]):
            raise ValueError('机器人授权配置无效：' + key)
    if not set(config['admin_user_ids']) <= set(config['allowed_user_ids']):
        raise ValueError('管理员必须同时为授权用户。')
    if type(config.get('bot_id')) is not int or config['bot_id'] <= 0 or type(config.get('chat_id')) is not int or config['chat_id'] == 0 or not re.fullmatch(r'[A-Za-z0-9_]+', config.get('bot_username', '')):
        raise ValueError('机器人身份或聊天配置无效。')
    return config


def permitted(config, user, permission='execute'):
    users = config['admin_user_ids'] if permission == 'admin' else config['allowed_user_ids']
    return user in users


def permitted_chat(config, chat_id, user_id=None):
    if type(chat_id) is not int or chat_id == 0:
        return False
    if chat_id > 0 and (type(user_id) is not int or user_id != chat_id):
        return False
    return chat_id == config['chat_id'] or (chat_id > 0 and permitted(config, user_id, 'admin'))


def permitted_message(config, message):
    user, chat = message.get('from') or {}, message.get('chat') or {}
    if message.get('sender_chat') or user.get('is_bot') or type(user.get('id')) is not int or user['id'] <= 0:
        return False
    chat_id = chat.get('id')
    if type(chat_id) is not int:
        return False
    if chat.get('type') == 'private':
        return chat_id > 0 and permitted_chat(config, chat_id, user['id'])
    return chat.get('type') in ('group', 'supergroup') and chat_id < 0 and permitted_chat(config, chat_id, user['id'])


def parse_command(text, username):
    parts = str(text).strip().split(maxsplit=1)
    if not parts or not parts[0].startswith('/'):
        return None
    name, sep, target = parts[0][1:].partition('@')
    if sep and target.lower() != username.lower():
        return None
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,31}', name):
        return None
    return name.lower(), parts[1].strip() if len(parts) == 2 else ''


def binding_candidate(update, username, challenge):
    message = update.get('message') or {}
    sender, chat = message.get('from') or {}, message.get('chat') or {}
    if message.get('sender_chat') or sender.get('is_bot') or type(sender.get('id')) is not int:
        return None
    if chat.get('type') not in ('private', 'group', 'supergroup'):
        return None
    if parse_command(message.get('text', ''), username) != ('bind', challenge):
        return None
    return message


def setup(layout, proxy=None):
    print('创建机器人：在官方 @BotFather 发送 /newbot。请为本应用使用独立 Bot。')
    print('Bot Token 仅在当前终端隐藏输入，勿发到群聊或本会话。')
    token = getpass.getpass('Bot Token: ').strip()
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise ValueError('Token 格式无效。')
    api = Telegram(token, proxy)
    bot = api.call('getMe')
    with BotLock(bot['id']):
        if api.call('getWebhookInfo').get('url'):
            raise RuntimeError('这个 Bot 已配置 Webhook。请使用新 Bot，或自行确认原服务后解除 Webhook。')
        challenge = secrets.token_hex(4)
        print('已验证机器人：@' + bot['username'])
        print('把机器人加入目标群，随后在该群发送：')
        print('/bind@' + bot['username'] + ' ' + challenge)
        print('也可在机器人私聊发送同样命令绑定私人聊天。默认群隐私模式可保持开启。')
        deadline, offset, message = time.monotonic() + 600, 0, None
        while time.monotonic() < deadline and message is None:
            for update in api.call('getUpdates', {'offset': offset, 'timeout': 20, 'allowed_updates': ['message']}):
                offset = max(offset, update['update_id'] + 1)
                candidate = binding_candidate(update, bot['username'], challenge)
                if not candidate:
                    continue
                chat, sender = candidate['chat'], candidate['from']
                print(f"发现聊天：{chat.get('title') or chat.get('first_name') or chat['id']} (ID {chat['id']})")
                print(f"绑定管理员：{sender.get('username') or sender.get('first_name') or sender['id']} (ID {sender['id']})")
                if input('确认绑定这个聊天和管理员？[y/N]: ').strip().lower() == 'y':
                    message = candidate
                    break
        if message is None:
            raise RuntimeError('等待绑定超时，尚未保存配置。')
        chat, owner = message['chat'], message['from']['id']
        api.call('getChat', {'chat_id': chat['id']})
        allowed, admins = [owner], [owner]
        if layout.config.exists():
            previous = load_config(layout)
            if previous['bot_id'] == bot['id'] and previous['chat_id'] == chat['id']:
                allowed = sorted(set(previous['allowed_user_ids'] + [owner]))
                admins = sorted(set(previous['admin_user_ids'] + [owner]))
        while True:
            extra = input('其他授权用户数字 ID（不要填 @用户名；直接回车跳过）: ').strip()
            if not extra:
                break
            values = re.split(r'[,，\s]+', extra)
            if any(not re.fullmatch(r'[1-9]\d*', v) for v in values):
                print('这里需要纯数字用户 ID，例如 123456789。现在可以直接回车跳过，之后由 /whoami 和 /allow 添加。')
                continue
            allowed = sorted(set(allowed + [int(v) for v in values]))
            break
        config = {'schema_version': 1, 'bot_id': bot['id'], 'bot_username': bot['username'],
                  'chat_id': chat['id'], 'chat_name': chat.get('title') or chat.get('first_name') or str(chat['id']),
                  'allowed_user_ids': allowed, 'admin_user_ids': admins, 'initial_offset': offset,
                  'command_max_age_seconds': 900, 'proxy': proxy, 'credentials': store_token(token, layout.root)}
        atomic_json(layout.config, config)
        api.send(chat['id'], '✅ Telegram Task Runner 绑定成功。管理员已登记。\n服务启动后发送 /help 查看命令；同事可发送 /whoami，然后由管理员 /allow 用户ID。')
        print('绑定完成。下一步安装自动启动服务，或运行 run 前台验证。')
        return 0
