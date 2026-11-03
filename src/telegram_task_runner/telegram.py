from __future__ import annotations
import json
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler


class ApiError(RuntimeError):
    def __init__(self, code, description, parameters=None):
        self.code, self.parameters = int(code), parameters or {}
        super().__init__(f'Telegram {code}: {description}')


class DeliveryUnknown(RuntimeError):
    """A transport error cannot prove whether a notification was delivered."""


class Telegram:
    def __init__(self, token, proxy=None):
        self.token = token
        self.opener = build_opener(ProxyHandler({'https': proxy}) if proxy else ProxyHandler())

    def call(self, method, payload=None, timeout=35):
        request = Request(f'https://api.telegram.org/bot{self.token}/{method}',
                          data=json.dumps(payload or {}).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'User-Agent': 'Telegram-Task-Runner/0.1'},
                          method='POST')
        try:
            with self.opener.open(request, timeout=timeout) as response:
                data = json.loads(response.read().decode('utf-8'))
        except HTTPError as error:
            try:
                body = json.loads(error.read().decode('utf-8'))
            except (ValueError, UnicodeError):
                raise DeliveryUnknown('Telegram HTTP 响应不明确') from None
            finally:
                error.close()
            raise ApiError(body.get('error_code', error.code), body.get('description', 'API error'), body.get('parameters')) from None
        except (URLError, TimeoutError, OSError, ValueError):
            raise DeliveryUnknown('Telegram 网络连接失败或响应超时；投递结果未确认') from None
        if not data.get('ok'):
            raise ApiError(data.get('error_code', 0), data.get('description', 'API error'), data.get('parameters'))
        return data.get('result')

    def send(self, chat_id, text, reply_to=None):
        payload = {'chat_id': chat_id, 'text': str(text)[:3900], 'link_preview_options': {'is_disabled': True}}
        if reply_to:
            payload['reply_parameters'] = {'message_id': reply_to, 'allow_sending_without_reply': True}
        return self.call('sendMessage', payload)
