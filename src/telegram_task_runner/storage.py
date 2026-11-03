"""Durable inbox, job/resource claims, reaction journal, cache GC, and notification outbox."""
from __future__ import annotations
import json
from pathlib import Path
import shutil
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
                status TEXT DEFAULT 'pending',due REAL DEFAULT 0,error TEXT,telegram_message_id INTEGER,
                job_id TEXT,attachment TEXT);
            CREATE TABLE IF NOT EXISTS reactions(
                chat_id INTEGER,message_id INTEGER,job_id TEXT,desired TEXT,applied TEXT,
                terminal INTEGER DEFAULT 0,retry INTEGER DEFAULT 0,updated REAL,
                PRIMARY KEY(chat_id,message_id));
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(outbox)').fetchall()}
        if 'job_id' not in columns:
            self.db.execute('ALTER TABLE outbox ADD COLUMN job_id TEXT')
        if 'attachment' not in columns:
            self.db.execute('ALTER TABLE outbox ADD COLUMN attachment TEXT')
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

    def update_job_spec(self, identifier, spec):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET spec=? WHERE id=?', (json.dumps(spec, ensure_ascii=False), identifier))

    def finish(self, identifier, value):
        with self.lock, self.db:
            self.db.execute('UPDATE jobs SET status=?,result=?,finished=? WHERE id=?', (value['status'], json.dumps(value, ensure_ascii=False), time.time(), identifier))
            self.db.execute('DELETE FROM resources WHERE job=?', (identifier,))

    def notify(self, key, chat, text, reply=None, *, job_id=None, attachment=None):
        attachment_json = json.dumps(attachment, ensure_ascii=False) if attachment else None
        with self.lock, self.db:
            self.db.execute(
                'INSERT OR IGNORE INTO outbox(event_key,chat_id,message_id,text,job_id,attachment) VALUES(?,?,?,?,?,?)',
                (key, chat, reply, str(text or '')[:3900], job_id, attachment_json),
            )

    def job_delivery_statuses(self, job_id):
        with self.lock:
            return [r[0] for r in self.db.execute('SELECT status FROM outbox WHERE job_id=?', (job_id,)).fetchall()]

    def set_reaction_intent(self, chat_id, message_id, emoji, *, job_id='', terminal=False):
        if not chat_id or not message_id:
            return False
        with self.lock, self.db:
            row = self.db.execute(
                'SELECT job_id,desired,applied,terminal,retry FROM reactions WHERE chat_id=? AND message_id=?',
                (chat_id, message_id),
            ).fetchone()
            if row and row['terminal'] and not terminal:
                return False
            if row and row['desired'] == emoji and row['applied'] == emoji and not row['retry']:
                return False
            applied = row['applied'] if row else ''
            self.db.execute(
                'INSERT OR REPLACE INTO reactions(chat_id,message_id,job_id,desired,applied,terminal,retry,updated) VALUES(?,?,?,?,?,?,0,?)',
                (chat_id, message_id, job_id or (row['job_id'] if row else ''), emoji, applied, 1 if terminal else 0, time.time()),
            )
            return True

    def mark_reaction_result(self, chat_id, message_id, emoji, *, ok: bool, permanent_error: bool = False):
        if not chat_id or not message_id:
            return
        with self.lock, self.db:
            row = self.db.execute(
                'SELECT desired,terminal FROM reactions WHERE chat_id=? AND message_id=?',
                (chat_id, message_id),
            ).fetchone()
            if not row or row['desired'] != emoji:
                return
            if ok:
                if row['terminal']:
                    self.db.execute('DELETE FROM reactions WHERE chat_id=? AND message_id=?', (chat_id, message_id))
                else:
                    self.db.execute(
                        'UPDATE reactions SET applied=?,retry=0,updated=? WHERE chat_id=? AND message_id=?',
                        (emoji, time.time(), chat_id, message_id),
                    )
            elif permanent_error:
                if row['terminal']:
                    self.db.execute('DELETE FROM reactions WHERE chat_id=? AND message_id=?', (chat_id, message_id))
                else:
                    self.db.execute('UPDATE reactions SET retry=0,updated=? WHERE chat_id=? AND message_id=?', (time.time(), chat_id, message_id))
            else:
                self.db.execute('UPDATE reactions SET retry=1,updated=? WHERE chat_id=? AND message_id=?', (time.time(), chat_id, message_id))

    def pending_reaction_retries(self, limit=20):
        with self.lock:
            rows = self.db.execute(
                'SELECT chat_id,message_id,job_id,desired,terminal FROM reactions WHERE retry=1 ORDER BY updated LIMIT ?',
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]

    def prune_jobs_cache(self, jobs_root: Path, *, incoming_bytes: int = 0, max_cache_mb: int = 512, max_jobs: int = 100) -> list[str]:
        if not jobs_root.is_dir():
            return []
        with self.lock:
            protected = {
                r[0] for r in self.db.execute(
                    "SELECT id FROM jobs WHERE status IN ('queued','running','push_pending','uncertain')"
                ).fetchall()
            }
            protected.update(
                r[0] for r in self.db.execute(
                    "SELECT DISTINCT job_id FROM outbox WHERE job_id IS NOT NULL AND status IN ('pending','retry','sending')"
                ).fetchall()
            )
            created_map = {
                r[0]: float(r[1] or 0)
                for r in self.db.execute('SELECT id,created FROM jobs').fetchall()
            }
        entries = []
        total_bytes = 0
        for folder in jobs_root.iterdir():
            if not folder.is_dir():
                continue
            try:
                size = sum(f.stat().st_size for f in folder.rglob('*') if f.is_file())
                mtime = folder.stat().st_mtime
            except OSError:
                continue
            total_bytes += size
            created = created_map.get(folder.name, mtime)
            entries.append((created, folder.name, folder, size))
        total_count = len(entries)
        evictable = [item for item in sorted(entries, key=lambda x: x[0]) if item[1] not in protected]
        max_bytes = max(1, int(max_cache_mb)) * 1024 * 1024
        max_jobs = max(1, int(max_jobs))
        removed = []
        while evictable and (total_bytes + max(0, int(incoming_bytes)) > max_bytes or total_count >= max_jobs):
            _, job_id, folder, size = evictable.pop(0)
            shutil.rmtree(folder, ignore_errors=True)
            total_bytes = max(0, total_bytes - size)
            total_count = max(0, total_count - 1)
            removed.append(job_id)
        return removed

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
