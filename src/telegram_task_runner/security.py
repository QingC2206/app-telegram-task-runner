"""Use OS-protected credentials; never fall back to plaintext storage."""
import hashlib
from .platforms import current


def keyring_backend():
    try:
        if current() == 'macos':
            from keyring.backends.macOS import Keyring
        elif current() == 'linux':
            from keyring.backends.SecretService import Keyring
        else:
            raise RuntimeError('Windows 使用 DPAPI。')
        backend = Keyring()
        if backend.priority <= 0:
            raise RuntimeError('系统凭据存储不可用。')
        return backend
    except ImportError:
        raise RuntimeError('请先运行本平台 install.sh，安装系统钥匙串适配依赖。') from None


def store_token(token, root):
    if current() == 'windows':
        from .platforms.windows_credentials import protect
        return {'backend': 'windows-dpapi', 'value': protect(token)}
    # Token rotation must not overwrite the previous credential before config replacement.
    account = hashlib.sha256((str(root.resolve()) + '\0' + token).encode()).hexdigest()
    keyring_backend().set_password('app-telegram-task-runner', account, token)
    return {'backend': current(), 'account': account}


def load_token(config):
    secret = config['credentials']
    if secret['backend'] == 'windows-dpapi' and current() == 'windows':
        from .platforms.windows_credentials import unprotect
        return unprotect(secret['value'])
    if secret['backend'] != current():
        raise RuntimeError('凭据属于其他平台，请在本机重新绑定。')
    value = keyring_backend().get_password('app-telegram-task-runner', secret['account'])
    if not value:
        raise RuntimeError('系统钥匙串中没有 Bot Token，请重新绑定。')
    return value
