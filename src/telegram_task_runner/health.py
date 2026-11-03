"""Read-only interpretation of a host heartbeat, independent of the native service manager."""
import json
import time


def interpret(value, now=None):
    now = time.time() if now is None else now
    result = {**value, 'heartbeat_age_seconds':max(0, round(now - value['heartbeat_at'],1))}
    telegram = dict(value.get('telegram', {}))
    success = telegram.get('last_poll_success_at',0)
    if telegram.get('consecutive_failures',0):
        connection = 'retrying'
    elif success and now - success <= 90:
        connection = 'online'
    elif now - value['started_at'] <= 90 and not success:
        connection = 'connecting'
    else:
        connection = 'stale'
    result['telegram'] = {**telegram,'state':connection}
    lifecycle = value.get('lifecycle','starting')
    if lifecycle in ('stopped','stopping'):
        result['state'] = lifecycle
    elif result['heartbeat_age_seconds'] > 20:
        result['state'] = 'stale'
    elif lifecycle == 'starting':
        result['state'] = 'starting'
    elif connection != 'online' or not all(value.get('workers', {}).values()):
        result['state'] = 'degraded'
    else:
        result['state'] = 'healthy'
    return result


def read(layout, now=None):
    path = layout.runtime / 'health.json'
    if not path.exists():
        return {'state':'unavailable','message':'尚无运行心跳；可能尚未启动或正在运行旧版本。'}
    try:
        return interpret(json.loads(path.read_text(encoding='utf-8')),now)
    except (ValueError,TypeError,KeyError):
        return {'state':'invalid','message':'运行心跳格式无效。'}
