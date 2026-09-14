#!/usr/bin/env python3
"""
Scraper Worker API — Flask сервер всередині Docker контейнера.

Запускається разом з Xvfb + VNC + noVNC.
Django викликає цей API щоб запустити scraper і отримати статус.
"""
import glob
import os
import subprocess
import sys
import threading
import time
from flask import Flask, jsonify, request

sys.path.insert(0, '/app')

app = Flask(__name__)
_lock = threading.Lock()

_state = {
    'running': False,
    'log':     '',
    'files':   [],
    'status':  'idle',   # idle | running | ok | partial | error
    'error':   '',
    'started': None,
    'finished': None,
}


# ── Health check ──────────────────────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'ok': True, 'display': os.environ.get('DISPLAY', '')})


# ── Статус поточного запуску ──────────────────────────────────────────────────

@app.route('/status')
def status():
    return jsonify(_state.copy())


# ── Запустити scraper ─────────────────────────────────────────────────────────

@app.route('/run', methods=['POST'])
def run():
    data = request.json or {}

    if not _lock.acquire(blocking=False):
        return jsonify({'error': 'Already running'}), 409

    site     = data.get('site', 'jlcpcb')
    username = data.get('username', '')
    password = data.get('password', '')
    start    = data.get('start', '')
    end      = data.get('end', '')
    output   = data.get('output', '/media/scraper')

    _state.update(
        running=True,
        log='',
        files=[],
        status='running',
        error='',
        started=time.strftime('%Y-%m-%d %H:%M:%S'),
        finished=None,
    )

    def _do():
        try:
            env = {**os.environ, 'DISPLAY': ':99'}
            cmd = [
                sys.executable, '/app/run.py', site,
                '--username', username,
                '--password', password,
                '--output',   output,
                '--start',    start,
                '--end',      end,
            ]
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
                bufsize=1,
            )
            lines = []
            for line in iter(proc.stdout.readline, ''):
                line = line.rstrip()
                if line:
                    lines.append(line)
                    _state['log'] = '\n'.join(lines[-200:])  # останні 200 рядків

            proc.wait()

            # Знаходимо завантажені файли
            pattern = os.path.join(output, site, 'invoice_*.pdf')
            _state['files']  = sorted(glob.glob(pattern))
            _state['status'] = 'ok' if proc.returncode == 0 and _state['files'] else (
                               'error' if proc.returncode != 0 else 'partial')

        except Exception as e:
            _state['error']  = str(e)
            _state['status'] = 'error'
        finally:
            _state['running']  = False
            _state['finished'] = time.strftime('%Y-%m-%d %H:%M:%S')
            _lock.release()

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({'started': True})


if __name__ == '__main__':
    print('[worker] API listening on 0.0.0.0:8888')
    app.run(host='0.0.0.0', port=8888, debug=False, threaded=True)
