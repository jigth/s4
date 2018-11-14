import argh
import time
import subprocess
import shell
import tempfile
import util.hacks
import os
import requests
import s4
import sys

if s4._debug:
    def _trace(f):
        def fn(*a, **kw):
            print(*a, kw if kw else '', file=sys.stderr)
            return f(*a, **kw)
        return fn
    requests.post = _trace(requests.post)
    requests.get = _trace(requests.get)

@s4.retry
def rm(prefix, recursive=False):
    assert 's3:' not in prefix
    assert prefix.startswith('s4://')
    if recursive:
        for address, port in s4.servers():
            resp = requests.post(f'http://{address}:{port}/delete?prefix={prefix}&recursive=true')
            assert resp.status_code == 200, resp
    else:
        server = s4.pick_server(prefix)
        resp = requests.post(f'http://{server}/delete?prefix={prefix}')
        assert resp.status_code == 200, resp

@s4.retry
def _ls(prefix, recursive):
    assert 's3:' not in prefix
    for address, port in s4.servers():
        url = f'http://{address}:{port}/list?prefix={prefix}&recursive={"true" if recursive else "false"}'
        resp = requests.get(url)
        assert resp.status_code == 200, resp
        yield from resp.json()

def ls(prefix, recursive=False):
    vals = sorted({f'  PRE {x}'
                   if x.endswith('/') else
                   f'_ _ _ {x}'
                   for x in _ls(prefix, recursive)})
    if not vals:
        sys.exit(1)
    return vals

@s4.retry
def cp(src, dst, recursive=False):
    assert 's3:' not in src + dst
    if recursive:
        if src.startswith('s4://'):
            bucket, *parts = src.split('s4://')[-1].rstrip('/').split('/')
            prefix = '/'.join(parts)
            for x in ls(src, recursive=True):
                key = x.split()[-1]
                token = os.path.dirname(prefix) if dst == '.' else prefix
                path = os.path.join(dst, key.split(token)[-1].lstrip(' /'))
                os.makedirs(os.path.dirname(path), 0o777, True)
                cp('s4://' + os.path.join(bucket, key), path)
        elif dst.startswith('s4://'):
            for dirpath, dirs, files in os.walk(src):
                path = dirpath.split(src)[-1].lstrip('/')
                for file in files:
                    cp(os.path.join(dirpath, file), os.path.join(dst, path, file))
    elif src.startswith('s4://') and dst.startswith('s4://'):
        print('there is no move, there is only cp and rm. -- yoda', file=sys.stderr)
        sys.exit(1)
    elif src.startswith('s4://'):
        resp = requests.post(f'http://localhost:{s4.http_port()}/new_port')
        assert resp.status_code == 200, resp
        port = int(resp.text)
        _, temp_path = tempfile.mkstemp(dir='.')
        try:
            if dst == '-':
                print('WARN: stdout is potentially slow, consider using a file path instead', file=sys.stderr)
                cmd = f'set -euo pipefail; nc -l {port} | xxhsum'
            else:
                cmd = f'set -euo pipefail; nc -l {port} | xxhsum > {temp_path}'
            start = time.time()
            proc = subprocess.Popen(cmd, shell=True, executable='/bin/bash', stderr=subprocess.PIPE)
            shell.run(f'while ! netstat -ln | grep {port}; do sleep .1; done', timeout=s4.timeout)
            server = s4.pick_server(src)
            resp = requests.post(f'http://{server}/prepare_get?key={src}&port={port}')
            assert resp.status_code == 200, resp
            uuid = resp.text
            while proc.poll() is None:
                assert time.time() - start < s4.timeout, f'timeout on cmd: {cmd}'
                time.sleep(.01)
            stderr = proc.stderr.read().decode('utf-8').rstrip()
            assert proc.poll() == 0, stderr
            checksum = stderr
            resp = requests.post(f'http://{server}/confirm_get?&uuid={uuid}&checksum={checksum}')
            assert resp.status_code == 200, resp
            if dst.endswith('/'):
                os.makedirs(dst, exist_ok=True)
                dst = os.path.join(dst, os.path.basename(src))
            if dst != '-':
                os.rename(temp_path, dst)
        finally:
            shell.run('rm -f', temp_path)
    elif dst.startswith('s4://'):
        if dst.endswith('/'):
            dst = os.path.join(dst, os.path.basename(src))
        server = s4.pick_server(dst)
        server_address = server.split(":")[0]
        resp = requests.post(f'http://{server}/prepare_put?key={dst}')
        assert resp.status_code == 200, resp
        uuid, port = resp.json()
        if src == '-':
            print('WARN: stdin is potentially slow, consider using a file path instead', file=sys.stderr)
            cmd = f'set -euo pipefail; xxhsum | nc -N {server_address} {port}'
            result = shell.run(cmd, stdin=sys.stdin, timeout=s4.timeout, warn=True)
        else:
            cmd = f'set -euo pipefail; cat {src} | xxhsum | nc -N {server_address} {port}'
            result = shell.run(cmd, timeout=s4.timeout, warn=True)
        assert result['exitcode'] == 0, result
        checksum = result['stderr']
        resp = requests.post(f'http://{server}/confirm_put?uuid={uuid}&checksum={checksum}')
        assert resp.status_code == 200, resp
    else:
        print('src or dst needs s4://')
        sys.exit(1)

def main():
    if util.hacks.override('--debug'):
        s4._debug = True
        shell.set_stream().__enter__()
    argh.dispatch_commands([cp, ls, rm])
