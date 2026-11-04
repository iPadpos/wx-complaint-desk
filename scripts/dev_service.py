"""本项目已配置开发环境的启停入口；只操作匹配目录与命令的本项目进程。"""
import argparse
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

parser = argparse.ArgumentParser()
parser.add_argument('action', choices=['start', 'stop', 'status'])
parser.add_argument('--config', required=True)
args = parser.parse_args()
config_file = Path(args.config).resolve()
cfg = json.loads(config_file.read_text())
if str(cfg.get('DEBUG', '')).lower() != 'true': raise SystemExit('此脚本仅用于已配置的开发环境')
repo = Path(__file__).resolve().parents[1]
runtime = config_file.parent
state_file = runtime / 'services.json'
port = int(cfg.get('DEV_WEB_PORT', 8316))
env = {**os.environ, 'COMPLAINT_CONFIG': str(config_file)}


def matches(pid, marker):
    try:
        os.kill(pid, 0)
        command = subprocess.check_output(['ps', '-p', str(pid), '-o', 'args='], text=True)
        return marker in command
    except (OSError, subprocess.CalledProcessError): return False


with (runtime / 'services.lock').open('a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    # 首次开发验证创建的独立MySQL进程也可由同一入口安全管理。
    mysql_pid = runtime / 'mysql.pid'
    if 'mysql' not in state and mysql_pid.exists():
        pid = int(mysql_pid.read_text())
        marker = f'--datadir={runtime / "mysql"}'
        if matches(pid, marker): state['mysql'] = {'pid': pid, 'marker': marker}
    if args.action == 'start':
        mysql_base = Path(cfg['DEV_MYSQL_BASE'])
        commands = {
            'mysql': ([str(mysql_base / 'bin/mysqld'), '--no-defaults', f'--basedir={mysql_base}',
                       f'--datadir={runtime / "mysql"}', '--bind-address=127.0.0.1', f'--port={cfg["DB_PORT"]}',
                       f'--socket={runtime / "mysql.sock"}', '--mysqlx=OFF', f'--pid-file={runtime / "mysql.pid"}',
                       f'--log-error={runtime / "mysql.log"}'], f'--datadir={runtime / "mysql"}'),
            'web': ([sys.executable, str(repo / 'manage.py'), 'runserver', f'127.0.0.1:{port}', '--noreload'],
                     f'{repo / "manage.py"} runserver 127.0.0.1:{port}'),
            'worker': ([sys.executable, str(repo / 'manage.py'), 'complaint_worker'],
                       f'{repo / "manage.py"} complaint_worker'),
        }
        for name, (command, marker) in commands.items():
            old = state.get(name)
            if old and matches(old['pid'], old['marker']): continue
            # 不抢占其他项目端口。
            if name in ('mysql', 'web'):
                import socket
                probe = socket.socket()
                # 快速重启时允许本项目连接的TIME_WAIT；仍拒绝已监听的其他服务。
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                try: probe.bind(('127.0.0.1', int(cfg['DB_PORT']) if name == 'mysql' else port))
                except OSError: raise SystemExit(f'{name} 端口已被占用，未修改其他进程')
                finally: probe.close()
            with (runtime / f'{name}.log').open('ab') as logfile:
                process = subprocess.Popen(command, cwd=repo, env=env, stdout=logfile, stderr=logfile, start_new_session=True)
            state[name] = {'pid': process.pid, 'marker': marker}
            state_file.write_text(json.dumps(state)); state_file.chmod(0o600)
            if name == 'mysql':
                for _ in range(100):
                    import socket
                    with socket.socket() as probe:
                        if probe.connect_ex(('127.0.0.1', int(cfg['DB_PORT']))) == 0: break
                    if process.poll() is not None: raise SystemExit('MySQL启动失败，请查看本项目mysql.log')
                    time.sleep(.1)
        for _ in range(100):
            try:
                with urlopen(f'http://127.0.0.1:{port}/health/', timeout=1) as response:
                    if json.load(response).get('status') == 'ok': break
            except Exception: time.sleep(.1)
        else: raise SystemExit('网页或数据库健康检查未通过，请查看本项目日志')
        print(f'开发工作台已就绪：http://127.0.0.1:{port}/（微信接口为演示或商户实际配置）')
    elif args.action == 'stop':
        for name in ('worker', 'web', 'mysql'):
            entry = state.get(name)
            if entry and matches(entry['pid'], entry['marker']):
                os.kill(entry['pid'], signal.SIGTERM)
                for _ in range(150):
                    if not matches(entry['pid'], entry['marker']): break
                    time.sleep(.1)
                else: raise SystemExit(f'{name}仍在退出，未强制终止；请检查本项目日志')
            state.pop(name, None)
            state_file.write_text(json.dumps(state))
        print('本项目开发服务已关闭，数据库内容已保留')
    else:
        for name in ('mysql', 'web', 'worker'):
            entry = state.get(name)
            live = bool(entry and matches(entry['pid'], entry['marker']))
            print(name, '运行中' if live else '已停止')
