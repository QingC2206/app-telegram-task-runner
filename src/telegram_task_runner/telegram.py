from __future__ import annotations
import hashlib
import json
import mimetypes
from pathlib import Path
import secrets
from urllib.error import HTTPError, URLError
from urllib.request import Request, build_opener, ProxyHandler


class ApiError(RuntimeError):
    def __init__(self, code, description, parameters=None):
        self.code, self.parameters = int(code), parameters or {}
        super().__init__(f'Telegram {code}: {description}')


class DeliveryUnknown(RuntimeError):
    """A transport error cannot prove whether a notification was delivered."""


class Telegram:
    def __init__(self, token, proxy=None, api_base_url=None):
        self.token = token
        self.api_base_url = (api_base_url or 'https://api.telegram.org').rstrip('/')
        self.is_local_server = self.api_base_url != 'https://api.telegram.org'
        self.opener = build_opener(ProxyHandler({'https': proxy, 'http': proxy}) if proxy else ProxyHandler())

    def _request_json(self, request: Request, timeout: int):
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

    def call(self, method, payload=None, timeout=35):
        request = Request(f'{self.api_base_url}/bot{self.token}/{method}',
                          data=json.dumps(payload or {}).encode('utf-8'),
                          headers={'Content-Type': 'application/json', 'User-Agent': 'Telegram-Task-Runner/0.1'},
                          method='POST')
        return self._request_json(request, timeout=timeout)

    def send(self, chat_id, text, reply_to=None):
        payload = {'chat_id': chat_id, 'text': str(text)[:3900], 'link_preview_options': {'is_disabled': True}}
        if reply_to:
            payload['reply_parameters'] = {'message_id': reply_to, 'allow_sending_without_reply': True}
        return self.call('sendMessage', payload)

    def react(self, chat_id, message_id, emoji, timeout=10):
        payload = {
            'chat_id': chat_id,
            'message_id': message_id,
            'reaction': [{'type': 'emoji', 'emoji': emoji}] if emoji else [],
            'is_big': False,
        }
        return self.call('setMessageReaction', payload, timeout=timeout)

    def download_file(self, file_id: str, destination: Path, max_bytes: int | None = None, timeout: int = 120) -> dict:
        info = self.call('getFile', {'file_id': file_id}, timeout=timeout)
        file_path = info.get('file_path') or ''
        declared_size = int(info.get('file_size') or 0)
        if max_bytes and declared_size > max_bytes:
            raise ValueError(f'文件大小 ({declared_size} B) 超过允许上限 ({max_bytes} B)')
        destination.parent.mkdir(parents=True, exist_ok=True)
        md5 = hashlib.md5()
        sha256 = hashlib.sha256()
        total = 0
        local_candidate = Path(file_path) if self.is_local_server and file_path else None
        if local_candidate and local_candidate.is_file():
            with local_candidate.open('rb') as src, destination.open('wb') as dst:
                while chunk := src.read(1024 * 1024):
                    total += len(chunk)
                    if max_bytes and total > max_bytes:
                        dst.close()
                        destination.unlink(missing_ok=True)
                        raise ValueError(f'下载文件大小超过允许上限 ({max_bytes} B)')
                    md5.update(chunk)
                    sha256.update(chunk)
                    dst.write(chunk)
        else:
            url = f'{self.api_base_url}/file/bot{self.token}/{file_path.lstrip("/")}'
            request = Request(url, headers={'User-Agent': 'Telegram-Task-Runner/0.1'}, method='GET')
            try:
                with self.opener.open(request, timeout=timeout) as response, destination.open('wb') as dst:
                    while chunk := response.read(1024 * 1024):
                        total += len(chunk)
                        if max_bytes and total > max_bytes:
                            dst.close()
                            destination.unlink(missing_ok=True)
                            raise ValueError(f'下载文件大小超过允许上限 ({max_bytes} B)')
                        md5.update(chunk)
                        sha256.update(chunk)
                        dst.write(chunk)
            except HTTPError as error:
                destination.unlink(missing_ok=True)
                error.close()
                raise ApiError(error.code, f'文件下载失败 (HTTP {error.code})') from None
            except (URLError, TimeoutError, OSError) as error:
                destination.unlink(missing_ok=True)
                raise DeliveryUnknown('Telegram 文件下载网络连接失败或超时') from error
        return {'size': total, 'md5': md5.hexdigest(), 'sha256': sha256.hexdigest(), 'file_path': file_path}

    def send_file(self, chat_id, path: Path, *, kind='document', filename=None, caption='', reply_to=None, timeout=180):
        method = 'sendPhoto' if kind == 'photo' else 'sendDocument'
        field_name = 'photo' if kind == 'photo' else 'document'
        display_name = filename or path.name
        content_type = mimetypes.guess_type(display_name)[0] or 'application/octet-stream'
        boundary = '----TelegramTaskRunnerBoundary' + secrets.token_hex(12)
        fields = {'chat_id': str(chat_id)}
        if caption:
            fields['caption'] = str(caption)[:900]
        if reply_to:
            fields['reply_parameters'] = json.dumps({'message_id': reply_to, 'allow_sending_without_reply': True})
        parts = []
        for key, val in fields.items():
            parts.append(
                f'--{boundary}\r\n'
                f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
                f'{val}\r\n'.encode('utf-8')
            )
        safe_header_name = display_name.replace('"', '_').replace('\r', '').replace('\n', '')
        parts.append(
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{field_name}"; filename="{safe_header_name}"\r\n'
            f'Content-Type: {content_type}\r\n\r\n'.encode('utf-8')
        )
        parts.append(path.read_bytes())
        parts.append(f'\r\n--{boundary}--\r\n'.encode('utf-8'))
        body = b''.join(parts)
        request = Request(
            f'{self.api_base_url}/bot{self.token}/{method}',
            data=body,
            headers={
                'Content-Type': f'multipart/form-data; boundary={boundary}',
                'Content-Length': str(len(body)),
                'User-Agent': 'Telegram-Task-Runner/0.1',
            },
            method='POST',
        )
        return self._request_json(request, timeout=timeout)

