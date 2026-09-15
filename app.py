#!/usr/bin/env python3
"""A turtle whose every step is a committed SQLite transaction. Stdlib only."""
import collections
import fcntl
import json
import logging
import os
from pathlib import Path
import random
import signal
import socket
import sqlite3
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DATA = Path(os.environ.get('DATA_DIR', '/data'))
DB = DATA / 'turtle.db'
POD = os.environ.get('POD_NAME', socket.gethostname())
NODE = os.environ.get('NODE_NAME', 'local')
PVC = os.environ.get('PVC_NAME', 'local directory')
INTERVAL = max(0.1, float(os.environ.get('STEP_SECONDS', '1')))
PORT = int(os.environ.get('PORT', '8080'))
STOP = threading.Event()
STATUS = {'error': None, 'commit_ms': 0, 'last_success': time.time()}
STATUS_LOCK = threading.Lock()
PAGE = Path(__file__).with_name('index.html').read_bytes()


def connect():
    db = sqlite3.connect(DB, timeout=2, isolation_level=None)
    # EXTRA also syncs the directory after deleting the rollback journal.
    db.execute('PRAGMA synchronous=EXTRA')
    db.execute('PRAGMA busy_timeout=2000')
    return db


def neighbours(grid, pos):
    x, y = pos
    for dx, dy in ((0, -1), (1, 0), (0, 1), (-1, 0)):
        nx, ny = x + dx, y + dy
        if 0 <= ny < len(grid) and 0 <= nx < len(grid[0]) and grid[ny][nx] == 0:
            yield (nx, ny)


def new_world():
    rng = random.Random(42)
    w, h = 25, 17
    grid = [[1] * w for _ in range(h)]
    grid[1][1] = 0
    stack = [(1, 1)]
    while stack:
        x, y = stack[-1]
        choices = [(x + dx, y + dy) for dx, dy in ((0, -2), (2, 0), (0, 2), (-2, 0))
                   if 0 < x + dx < w - 1 and 0 < y + dy < h - 1 and grid[y + dy][x + dx]]
        if not choices:
            stack.pop()
            continue
        nx, ny = rng.choice(choices)
        grid[(y + ny) // 2][(x + nx) // 2] = 0
        grid[ny][nx] = 0
        stack.append((nx, ny))
    # Short loops make the garden pleasant to watch without changing reachability.
    for _ in range(18):
        x, y = rng.randrange(1, w - 1), rng.randrange(1, h - 1)
        if grid[y][x] and ((grid[y][x-1] == grid[y][x+1] == 0) or
                           (grid[y-1][x] == grid[y+1][x] == 0)):
            grid[y][x] = 0
    cells = [[x, y] for y in range(h) for x in range(w) if grid[y][x] == 0 and (x, y) != (1, 1)]
    # Give the opening demonstration an early reward.
    near = list(neighbours(grid, (1, 1)))[0]
    lettuce = [list(near)] + rng.sample([p for p in cells if p != list(near)], 7)
    return {'version': 1, 'garden_id': str(uuid.uuid4())[:8], 'grid': grid,
            'position': [1, 1], 'direction': 'right', 'lettuce': lettuce,
            'trail': [[1, 1]], 'steps': 0, 'collected': 0, 'sessions': 0,
            'created_at': time.time(), 'saved_at': None, 'writer_pod': None}


def advance(world):
    start = tuple(world['position'])
    targets = {tuple(p) for p in world['lettuce']}
    queue = collections.deque([start])
    previous = {start: None}
    goal = None
    while queue:
        p = queue.popleft()
        if p in targets:
            goal = p
            break
        for n in neighbours(world['grid'], p):
            if n not in previous:
                previous[n] = p
                queue.append(n)
    if goal is None:
        raise ValueError('No reachable lettuce; refusing to reset persisted state')
    p = goal
    while previous[p] is not None and previous[p] != start:
        p = previous[p]
    dx, dy = p[0] - start[0], p[1] - start[1]
    world['direction'] = {(1, 0): 'right', (-1, 0): 'left', (0, 1): 'down', (0, -1): 'up'}[(dx, dy)]
    world['position'] = list(p)
    world['trail'] = (world['trail'] + [list(p)])[-100:]
    world['steps'] += 1
    kind = 'step'
    if list(p) in world['lettuce']:
        world['lettuce'].remove(list(p))
        world['collected'] += 1
        kind = 'lettuce'
        choices = [[x, y] for y, row in enumerate(world['grid']) for x, value in enumerate(row)
                   if value == 0 and [x, y] not in world['lettuce'] and (x, y) != p]
        world['lettuce'].append(random.choice(choices))
    return kind


def store(db, world, kind):
    world['saved_at'] = time.time()
    world['writer_pod'] = POD
    payload = json.dumps(world, separators=(',', ':'))
    db.execute('INSERT OR REPLACE INTO world(id, payload) VALUES(1, ?)', (payload,))
    db.execute('INSERT INTO events(step, kind, saved_at, pod, lettuce) VALUES(?,?,?,?,?)',
               (world['steps'], kind, world['saved_at'], POD, world['collected']))
    # Bounded audit trail; the lifetime totals stay in world.
    db.execute('DELETE FROM events WHERE id < (SELECT MAX(id) - 299 FROM events)')


def initialise():
    DATA.mkdir(parents=True, exist_ok=True)
    # Keep descriptor alive for the life of the process. Never unlink this file.
    lock = (DATA / 'writer.lock').open('a')
    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    db = connect()
    try:
        db.execute('PRAGMA journal_mode=DELETE')
        db.execute('BEGIN IMMEDIATE')
        db.execute('CREATE TABLE IF NOT EXISTS world(id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        db.execute('CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT, step INTEGER, kind TEXT, saved_at REAL, pod TEXT, lettuce INTEGER)')
        row = db.execute('SELECT payload FROM world WHERE id=1').fetchone()
        world = json.loads(row[0]) if row else new_world()
        if world['version'] != 1:
            raise ValueError('Unsupported saved state version')
        world['sessions'] += 1
        store(db, world, 'resume' if row else 'start')
        db.execute('COMMIT')
        logging.info('Opened garden %s at step %d; lettuces=%d; session=%d',
                     world['garden_id'], world['steps'], world['collected'], world['sessions'])
    finally:
        db.close()
    return lock


def writer():
    while not STOP.wait(INTERVAL):
        db = None
        try:
            began = time.perf_counter()
            db = connect()
            db.execute('BEGIN IMMEDIATE')
            # Always reload the last committed state, including after any I/O error.
            world = json.loads(db.execute('SELECT payload FROM world WHERE id=1').fetchone()[0])
            kind = advance(world)
            store(db, world, kind)
            db.execute('COMMIT')
            elapsed = round((time.perf_counter() - began) * 1000, 2)
            with STATUS_LOCK:
                STATUS.update(error=None, commit_ms=elapsed, last_success=time.time())
            logging.info('COMMITTED step=%d lettuce=%d position=%s pod=%s %.2fms',
                         world['steps'], world['collected'], world['position'], POD, elapsed)
        except Exception as exc:
            if db is not None and db.in_transaction:
                try:
                    db.execute('ROLLBACK')
                except sqlite3.Error:
                    pass
            with STATUS_LOCK:
                STATUS['error'] = str(exc)
            logging.exception('Write failed; retaining committed state and retrying')
        finally:
            if db is not None:
                db.close()


class Handler(BaseHTTPRequestHandler):
    def reply(self, code, body, content_type='application/json'):
        data = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(code)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        path = self.path.split('?', 1)[0]
        if path == '/':
            return self.reply(200, PAGE, 'text/html; charset=utf-8')
        if path == '/healthz':
            return self.reply(200 if self.server.writer_thread.is_alive() else 503, {'alive': self.server.writer_thread.is_alive()})
        if path not in ('/api/state', '/readyz'):
            return self.reply(404, {'error': 'Not found'})
        db = None
        try:
            db = connect()
            db.execute('BEGIN')
            world = json.loads(db.execute('SELECT payload FROM world WHERE id=1').fetchone()[0])
            events = [dict(zip(('step', 'kind', 'saved_at', 'pod', 'lettuce'), r)) for r in
                      db.execute('SELECT step, kind, saved_at, pod, lettuce FROM events ORDER BY id DESC LIMIT 8')]
            db.execute('COMMIT')
            if path == '/readyz':
                return self.reply(200, {'ready': True})
            with STATUS_LOCK:
                status = dict(STATUS)
            return self.reply(200, {'world': world, 'events': events, 'runtime': {
                'pod': POD, 'node': NODE, 'pvc': PVC, 'interval': INTERVAL,
                'db_bytes': DB.stat().st_size, 'server_time': time.time(), **status}})
        except Exception:
            logging.exception('Could not read persisted state')
            return self.reply(503, {'error': 'Persistent storage unavailable; no new state is being displayed.'})
        finally:
            if db is not None:
                db.close()

    def log_message(self, *_):
        pass


def main():
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(message)s')
    writer_lock = initialise()
    thread = threading.Thread(target=writer, name='sqlite-writer', daemon=True)
    server = ThreadingHTTPServer(('0.0.0.0', PORT), Handler)
    server.writer_thread = thread
    server.timeout = 0.5
    signal.signal(signal.SIGTERM, lambda *_: STOP.set())
    signal.signal(signal.SIGINT, lambda *_: STOP.set())
    thread.start()
    logging.info('Listening on :%d; writing to %s every %.2fs', PORT, DB, INTERVAL)
    try:
        while not STOP.is_set():
            server.handle_request()
    finally:
        STOP.set()
        thread.join(timeout=5)
        server.server_close()
        writer_lock.close()


if __name__ == '__main__':
    main()
