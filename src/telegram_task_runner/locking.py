"""Independent root and bot locks; no shared port with the Codex bridge."""
import ctypes
from ctypes import wintypes
import os
from pathlib import Path


class FileLock:
    def __init__(self, path: Path):
        self.path, self.stream = path, None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.path.open('a+b')
        if not self.path.stat().st_size:
            self.stream.write(b'0'); self.stream.flush()
        self.stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.stream.close()
            raise RuntimeError('这个应用实例已经运行，请先停止它。') from None
        return self

    def __exit__(self, *args):
        if os.name == 'nt':
            import msvcrt
            self.stream.seek(0)
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        self.stream.close()


class BotLock:
    def __init__(self, identifier):
        self.identifier, self.handle = str(identifier), None

    def __enter__(self):
        if os.name != 'nt':
            self.posix = FileLock(Path.home() / '.cache' / 'app-telegram-task-runner' / ('bot-' + self.identifier + '.lock'))
            self.posix.__enter__()
            return self
        self.api = ctypes.WinDLL('kernel32', use_last_error=True)
        self.api.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
        self.api.CreateMutexW.restype = wintypes.HANDLE
        self.api.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.api.CreateMutexW(None, False, 'Global\\AppTelegramTaskRunnerBot_' + self.identifier)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == 183:
            self.api.CloseHandle(self.handle)
            self.handle = None
            raise RuntimeError('本机已有程序使用这个机器人，请停止它或使用独立 Bot。')
        return self

    def __exit__(self, *args):
        if os.name != 'nt':
            return self.posix.__exit__(*args)
        if self.handle:
            self.api.CloseHandle(self.handle)
