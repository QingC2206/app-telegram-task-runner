"""Portable service facade and native definitions; no global Python or Git changes."""
from __future__ import annotations
import getpass
import hashlib
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from ..binding import load_config
from . import current
from ..security import load_token
from ..util import atomic_json


def identity(root):
    return 'app-telegram-task-runner-' + hashlib.sha256(str(root.resolve()).encode()).hexdigest()[:12]


def arguments(layout, python=None):
    return [str(python or Path(sys.executable)), str(layout.root / 'runner.py'), '--root', str(layout.root), 'run']


def windows_xml(argv, root, account, mode='logon'):
    if mode != 'logon':
        raise ValueError('Windows 仅支持当前用户登录后自动启动。')
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    task = ET.Element('{' + ns + '}Task', version='1.4')
    def add(parent, name, text=None, **attrs):
        node = ET.SubElement(parent, '{' + ns + '}' + name, attrs)
        node.text = text
        return node
    registration = add(task, 'RegistrationInfo')
    add(registration, 'Description', 'Managed by ' + identity(root) + '; root=' + str(root))
    triggers = add(task, 'Triggers')
    login = add(triggers, 'LogonTrigger'); add(login, 'Enabled', 'true'); add(login, 'UserId', account)
    principals = add(task, 'Principals'); principal = add(principals, 'Principal', id='Author')
    add(principal, 'UserId', account)
    add(principal, 'LogonType', 'InteractiveToken')
    add(principal, 'RunLevel', 'LeastPrivilege')
    settings = add(task, 'Settings')
    for name, value in (('MultipleInstancesPolicy','IgnoreNew'),('DisallowStartIfOnBatteries','false'),
                        ('StopIfGoingOnBatteries','false'),('StartWhenAvailable','true'),('ExecutionTimeLimit','PT0S')):
        add(settings, name, value)
    restart = add(settings, 'RestartOnFailure'); add(restart, 'Interval', 'PT1M'); add(restart, 'Count', '999')
    action = add(add(task, 'Actions', Context='Author'), 'Exec')
    add(action, 'Command', argv[0]); add(action, 'Arguments', subprocess.list2cmdline(argv[1:])); add(action, 'WorkingDirectory', str(root))
    return ET.tostring(task, encoding='unicode')


def macos_definition(layout, argv):
    label = 'com.qingc.' + identity(layout.root)
    return {'Label':label, 'ProgramArguments':argv, 'WorkingDirectory':str(layout.root),
            'RunAtLoad':True, 'KeepAlive':True, 'ThrottleInterval':30,
            'StandardOutPath':str(layout.runtime / 'service.stdout.log'),
            'StandardErrorPath':str(layout.runtime / 'service.stderr.log')}


def systemd_quote(value, expand_environment=True):
    if '\n' in value or '\r' in value or '\0' in value:
        raise ValueError('服务参数中不能包含换行或 NUL。')
    value = value.replace('\\','\\\\').replace('"','\\"').replace('%','%%')
    if expand_environment:
        value = value.replace('$','$$')
    return '"' + value + '"'


def linux_definition(layout, argv):
    return '\n'.join([
        '# Managed by ' + identity(layout.root), '[Unit]', 'Description=Telegram Task Runner',
        'After=network-online.target', '', '[Service]', 'Type=simple',
        'WorkingDirectory=' + systemd_quote(str(layout.root), expand_environment=False),
        'ExecStart=' + ' '.join(systemd_quote(v) for v in argv),
        'Restart=on-failure', 'RestartSec=15', 'TimeoutStopSec=45', 'KillMode=control-group',
        'Environment=PYTHONUNBUFFERED=1', '', '[Install]', 'WantedBy=default.target', ''])


def manage(layout, action, mode='logon'):
    platform = current()
    if action == 'install':
        load_token(load_config(layout))
    if platform == 'windows':
        python = Path(sys.executable)
        if python.with_name('pythonw.exe').exists():
            python = python.with_name('pythonw.exe')
        spec = {'root':str(layout.root), 'name':identity(layout.root), 'argv':arguments(layout, python), 'mode':mode}
        spec['arguments'] = subprocess.list2cmdline(spec['argv'][1:])
        atomic_json(layout.runtime / 'service-spec.json', spec)
        powershell = shutil.which('pwsh') or shutil.which('powershell')
        if not powershell:
            raise RuntimeError('没有找到 PowerShell。')
        return subprocess.run([powershell,'-NoProfile','-ExecutionPolicy','Bypass','-File',str(layout.root/'tools'/'Manage-Service.ps1'),
                               '-Action',action,'-SpecPath',str(layout.runtime/'service-spec.json')], check=False).returncode
    argv = arguments(layout)
    if platform == 'macos':
        definition = macos_definition(layout, argv)
        label = definition['Label']
        path = Path.home() / 'Library' / 'LaunchAgents' / (label + '.plist')
        if path.is_symlink():
            raise RuntimeError('服务定义不可为符号链接。')
        if path.exists() and plistlib.loads(path.read_bytes()).get('ProgramArguments') != argv:
            raise RuntimeError('现有 LaunchAgent 不属于本应用实例。')
        target = f'gui/{os.getuid()}'
        command = lambda *args, check=True: subprocess.run(['launchctl', *args], check=check).returncode
        if action == 'status':
            return command('print', target+'/'+label, check=False)
        if action == 'install':
            path.parent.mkdir(parents=True, exist_ok=True)
            command('bootout', target, str(path), check=False)
            path.write_bytes(plistlib.dumps(definition))
            command('enable', target+'/'+label)
            return command('bootstrap', target, str(path))
        if action == 'start':
            if not path.exists():
                raise RuntimeError('请先安装服务。')
            command('enable', target+'/'+label)
            command('bootstrap', target, str(path), check=False)
            return command('kickstart', target+'/'+label)
        if action in ('stop','uninstall'):
            command('disable', target+'/'+label)
            command('bootout', target, str(path), check=False)
            if action == 'uninstall':
                path.unlink(missing_ok=True)
            return 0
    if not shutil.which('systemctl'):
        raise RuntimeError('Linux 自动启动需要 systemd 用户服务；也可先使用 run 前台运行。')
    label = identity(layout.root) + '.service'
    path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'systemd' / 'user' / label
    definition = linux_definition(layout, argv)
    if path.is_symlink() or (path.exists() and path.read_text(encoding='utf-8') != definition):
        raise RuntimeError('现有 systemd 单元不属于本应用定义，请先核对。')
    command = lambda *args, check=True: subprocess.run(['systemctl','--user',*args], check=check).returncode
    if action == 'status':
        return command('status', label, '--no-pager', check=False)
    if action == 'install':
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(definition, encoding='utf-8')
        command('daemon-reload')
        return command('enable','--now',label)
    if action == 'uninstall':
        command('disable','--now',label, check=False)
        path.unlink(missing_ok=True)
        return command('daemon-reload')
    if action == 'stop':
        return command('disable','--now',label)
    return command('enable','--now',label)
