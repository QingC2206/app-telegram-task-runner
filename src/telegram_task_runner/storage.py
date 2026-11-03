"""Durable inbox, job/resource claims, and notification outbox."""
from __future__ import annotations
import json
from pathlib import Path
import sqlite3
import threading
import time


class Store:
    def __init__(self, path: Path):
        self.lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False, timeout=10)
        path.chmod(0o600)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA busy_timeout=10000')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY,payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS jobs(
                id TEXT PRIMARY KEY,update_id INTEGER UNIQUE,plugin TEXT,revision TEXT,command TEXT,
                argument TEXT,mode TEXT,permission TEXT,user_id INTEGER,chat_id INTEGER,message_id INTEGER,
                status TEXT,spec TEXT,created REAL,started REAL,finished REAL,progress TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS resources(name TEXT PRIMARY KEY,job TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox(
                id INTEGER PRIMARY KEY,event_key TEXT UNIQUE,chat_id INTEGER,message_id INTEGER,text TEXT,
                status TEXT DEFAULT 'pending',due REAL DEFAULT 0,error TEXT,telegram_message_id INTEGER);
        ''')
        self.db.commit()

    def get(self, key, default=None):
        with self.lock:
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.lock, self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', (key, json.dumps(value)))

    def receive(self, update):
        identifier = int(update['update_id'])
        with self.lock, self.db:
            if identifier < self.get('offset', 0):
                return
            self.db.execute('INSERT OR IGNORE INTO inbox VALUES(?,?)', (identifier, json.dumps(update)))
            self.db.execute('INSERT OR REPLACE INTO meta VALUES(?,?)', ('offset', json.dumps(identifier + 1)))

    def incoming(self):
        with self.lock:
            return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM inbox ORDER BY id LIMIT 100')]

    def handled(self, identifier):
        with self.lock, self.db:
            self.db.execute('DELETE FROM inbox WHERE id=?', (identifier,))

    def for_update(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE update_id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def add_job(self, value):
        columns = ('id','update_id','plugin','revision','command','argument','mode','permission','user_id','chat_id','message_id','status','spec','created')
        value = {**value, 'status': 'queued', 'created': time.time(), 'spec': json.dumps(value['spec'])}
        with self.lock, self.db:
            self.db.execute('INSERT INTO jobs(' + ','.join(columns) + ') VALUES(' + ','.join('?' for _ in columns) + ')', tuple(value[c] for c in columns))

    def job(self, identifier):
        with self.lock:
            row = self.db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def jobs(self, limit=10, *, chat_id=None):
        with self.lock:
            if chat_id is None:
                rows = self.db.execute('SELECT * FROM jobs ORDER BY created DESC LIMIT ?', (limit,))
            else:
                rows = self.db.execute('SELECT * FROM jobs WHERE chat_id=? ORDER BY created DESC LIMIT ?', (chat_id,limit))
            return [dict(r) for r in rows]

    def claim(self, mode):
        with self.lock, self.db:
            for row in self.db.execute("SELECT * FROM jobs WHERE status='queued' AND mode=? ORDER BY created", (mode,)).fetchall():
                resources = json.loads(row['spec']).get('resources', [])
                if any(self.db.execute('SELECT 1 FROM resources WHERE name=?', (name,)).fetchone() for name in resources):
                    continue
                for name in sorted(set(resources)):
                    self.db.execute('INSERT INTO resources VALUES(?,?)', (name, row['id']))
                self.db.execute("UPDATE jobs SET status='running',started=? WHERE id=?", (time.time(), row['id']))
                return self.job(row['id'])
        return None

    def progress(self, identifier, text):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET progress=? WHERE id=?', (text[:1000], identifier))

    def finish(self, identifier, value):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET status=?,result=?,finished=? WHERE id=?', (value['status'], json.dumps(value, ensure_ascii=False), time.time(), identifier))
            self.db.execute('DELETE FROM resources WHERE job=?', (identifier,))

    def notify(self, key, chat, text, reply=None):
        with self.lock, self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox(event_key,chat_id,message_id,text) VALUES(?,?,?,?)', (key, chat, reply, text[:3900]))

    def take_notification(self):
        with self.lock, self.db:
            row = self.db.execute("SELECT * FROM outbox WHERE status IN ('pending','retry') AND due<=? ORDER BY id LIMIT 1", (time.time(),)).fetchone()
            if row:
                self.db.execute("UPDATE outbox SET status='sending' WHERE id=?", (row['id'],))
                return dict(row)
        return None

    def delivered(self, identifier, status, *, error='', due=0, telegram_id=None):
        with self.lock, self.db:
            self.db.execute('UPDATE outbox SET status=?,error=?,due=?,telegram_message_id=? WHERE id=?', (status, error[:500], due, telegram_id, identifier))

    def recover(self):
        with self.lock, self.db:
            active = self.db.execute("SELECT id FROM jobs WHERE status='running'").fetchall()
            for row in active:
                self.finish(row[0], {'ok': False, 'status': 'uncertain', 'message': '服务中断，结果需要核对；未自动重跑。请查看任务日志，并按插件提供的恢复方式处理。', 'data': {}})
            self.db.execute('DELETE FROM resources')
            self.db.execute("UPDATE outbox SET status='unknown',error='服务中断，通知投递未确认' WHERE status='sending'")
        return len(active)

    def counts(self):
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute('SELECT status,count(*) FROM jobs GROUP BY status')}

    def notification_counts(self):
        with self.lock:
            return {r[0]: r[1] for r in self.db.execute('SELECT status,count(*) FROM outbox GROUP BY status')}

    def close(self):
        self.db.close()
