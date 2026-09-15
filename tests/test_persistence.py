import importlib.util
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.data = Path(self.temp.name)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            process.stdout.close()
        self.temp.cleanup()

    def spawn(self, pod, interval='0.1'):
        port = free_port()
        env = dict(os.environ, DATA_DIR=str(self.data), POD_NAME=pod,
                   STEP_SECONDS=interval, PORT=str(port))
        proc = subprocess.Popen([sys.executable, str(ROOT / 'app.py')], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        self.processes.append(proc)
        return proc, port

    def state(self, port):
        with urlopen(f'http://127.0.0.1:{port}/api/state', timeout=2) as response:
            return json.load(response)['world']

    def wait_state(self, proc, port, predicate=lambda _: True):
        deadline = time.monotonic() + 8
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                self.fail(proc.stdout.read().decode())
            try:
                state = self.state(port)
                if predicate(state):
                    return state
            except OSError:
                pass
            time.sleep(0.04)
        self.fail('Server never reached expected persisted state')

    def test_hard_kill_restores_exact_committed_world(self):
        proc, port = self.spawn('turtle-before')
        self.wait_state(proc, port, lambda w: w['collected'] >= 1 and w['steps'] >= 4)
        proc.kill()
        proc.wait(timeout=3)
        with sqlite3.connect(self.data / 'turtle.db') as db:
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
            committed = json.loads(db.execute('SELECT payload FROM world').fetchone()[0])
        new_proc, new_port = self.spawn('turtle-after', '60')
        resumed = self.wait_state(new_proc, new_port)
        for key in committed:
            if key not in ('saved_at', 'sessions', 'writer_pod'):
                self.assertEqual(resumed[key], committed[key], key)
        self.assertEqual(resumed['sessions'], committed['sessions'] + 1)
        self.assertEqual(resumed['writer_pod'], 'turtle-after')

    def test_second_writer_is_refused(self):
        proc, port = self.spawn('owner', '60')
        before = self.wait_state(proc, port)
        second, _ = self.spawn('intruder')
        self.assertNotEqual(second.wait(timeout=4), 0)
        self.assertIn('BlockingIOError', second.stdout.read().decode())
        after = self.state(port)
        self.assertEqual(after, before)

    def module(self):
        spec = importlib.util.spec_from_file_location('turtle_app_test', ROOT / 'app.py')
        app = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(app)
        app.DATA = self.data
        app.DB = self.data / 'turtle.db'
        app.INTERVAL = 0.1
        return app

    def test_read_only_storage_never_advances_committed_state(self):
        app = self.module()
        lock = app.initialise()
        original_connect = app.connect
        with original_connect() as db:
            before = db.execute('SELECT payload FROM world').fetchone()[0]

        def read_only():
            db = original_connect()
            db.execute('PRAGMA query_only=ON')
            return db

        thread = threading.Thread(target=app.writer)
        try:
            with patch.object(app, 'connect', read_only):
                thread.start()
                deadline = time.monotonic() + 3
                while not app.STATUS['error'] and time.monotonic() < deadline:
                    time.sleep(0.02)
                app.STOP.set()
                thread.join(timeout=3)
            self.assertIn('readonly', app.STATUS['error'])
            with original_connect() as db:
                self.assertEqual(db.execute('SELECT payload FROM world').fetchone()[0], before)
        finally:
            app.STOP.set()
            thread.join(timeout=3)
            lock.close()

    def test_long_run_legal_moves_and_bounded_audit(self):
        app = self.module()
        lock = app.initialise()
        db = app.connect()
        try:
            for step in range(1, 351):
                db.execute('BEGIN IMMEDIATE')
                world = json.loads(db.execute('SELECT payload FROM world').fetchone()[0])
                previous = world['position'][:]
                kind = app.advance(world)
                x, y = world['position']
                self.assertEqual(abs(x-previous[0]) + abs(y-previous[1]), 1)
                self.assertEqual(world['grid'][y][x], 0)
                self.assertEqual(world['steps'], step)
                self.assertEqual(len({tuple(p) for p in world['lettuce']}), 8)
                app.store(db, world, kind)
                db.execute('COMMIT')
            events = db.execute('SELECT step FROM events ORDER BY id').fetchall()
            self.assertEqual([r[0] for r in events], list(range(51, 351)))
            self.assertGreater(world['collected'], 8)
            self.assertEqual(db.execute('PRAGMA integrity_check').fetchone()[0], 'ok')
        finally:
            db.close()
            lock.close()


if __name__ == '__main__':
    unittest.main()
