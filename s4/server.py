#!/usr/bin/env pypy3
import argh
import concurrent.futures
import json
import logging
import os
import random
import s4
import shell
import subprocess
import sys
import tempfile
import time
import tornado.gen
import tornado.ioloop
import util.hacks
import util.log
import uuid
import web

ports_in_use = set()
jobs = {}
path_prefix = '_s4_data'
max_jobs = int(os.environ.get('S4_MAX_JOBS', 10))
nc_pool = concurrent.futures.ThreadPoolExecutor(max_jobs)

def new_uuid():
    for _ in range(10):
        val = str(uuid.uuid4())
        if val not in jobs:
            jobs[val] = '::taken'
            return val
    assert False

def new_port():
    for _ in range(10):
        port = random.randint(20000, 60000)
        if port not in ports_in_use:
            ports_in_use.add(port)
            return port
    assert False

def return_port(port):
    ports_in_use.remove(port)

async def prepare_put_handler(req):
    if len(jobs) > max_jobs:
        return {'status': 429}
    else:
        key = req['query']['key']
        assert '0.0.0.0' == s4.pick_server(key).split(':')[0] # make sure the key is meant to live on this server before accepting it
        path = os.path.join(path_prefix, key.split('s4://')[-1])
        parent = os.path.dirname(path)
        _, temp_path = tempfile.mkstemp(dir='.')
        port = new_port()
        os.makedirs(parent, exist_ok=True)
        cmd = f'nc -l {port} | xxh3 --stream > {temp_path}'
        uuid = new_uuid()
        jobs[uuid] = {'time': time.monotonic(),
                      'future': submit(shell.run, cmd, timeout=s4.timeout, warn=True, executor=nc_pool),
                      'temp_path': temp_path,
                      'path': path}
        await submit(shell.run, f'timeout {s4.timeout} bash -c "while ! netstat -ln | grep {port}; do sleep .1; done"')
        return {'status': 200, 'body': json.dumps([uuid, port])}

async def confirm_put_handler(req):
    uuid = req['query']['uuid']
    checksum = req['query']['checksum']
    job = jobs.pop(uuid)
    result = await job['future']
    assert result['exitcode'] == 0, result
    local_checksum = result['stderr']
    assert checksum == local_checksum, [checksum, local_checksum, result]
    with open(f'{job["path"]}.xxh3', 'w') as f:
        f.write(checksum)
    await submit(os.rename, job['temp_path'], job['path'])
    return {'status': 200}

async def prepare_get_handler(req):
    key = req['query']['key']
    port = req['query']['port']
    remote = req['remote']
    assert '0.0.0.0' == s4.pick_server(key).split(':')[0]  # make sure the key is meant to live on this server before accepting it
    path = os.path.join(path_prefix, key.split('s4://')[-1])
    cmd = f'xxh3 --stream < {path} | nc -N {remote} {port}'
    uuid = new_uuid()
    jobs[uuid] = {'time': time.monotonic(),
                  'future': submit(shell.run, cmd, timeout=s4.timeout, warn=True, executor=nc_pool),
                  'path': path}
    return {'status': 200, 'body': uuid}

async def confirm_get_handler(req):
    uuid = req['query']['uuid']
    read_checksum = req['query']['checksum']
    job = jobs.pop(uuid)
    result = await job['future']
    assert result['exitcode'] == 0, result
    local_checksum = result['stderr']
    with open(f'{job["path"]}.xxh3') as f:
        checksum = f.read()
    assert checksum == local_checksum, [checksum, local_checksum]
    assert read_checksum == local_checksum, [read_checksum, local_checksum]
    return {'status': 200}

async def list_handler(req):
    _prefix = prefix = req['query'].get('prefix', '').split('s4://')[-1]
    prefix = os.path.join(path_prefix, prefix)
    recursive = req['query'].get('recursive') == 'true'
    try:
        if recursive:
            if not prefix.endswith('/'):
                prefix += '*'
            res = await submit(shell.run, f"find {prefix} -type f ! -name '*.xxh3'", warn=True, echo=True)
            assert res['exitcode'] == 0 or 'No such file or directory' in res['stderr']
            xs = res['stdout'].splitlines()
            xs = ['/'.join(x.split('/')[2:]) for x in xs]
        else:
            name = ''
            if not prefix.endswith('/'):
                name = os.path.basename(prefix)
                name = f"-name '{name}*'"
                prefix = os.path.dirname(prefix)
            res = await submit(shell.run, f"find {prefix} -maxdepth 1 -type f ! -name '*.xxh3' {name}", warn=True)
            assert res['exitcode'] == 0 or 'No such file or directory' in res['stderr']
            files = res['stdout']
            res = await submit(shell.run, f"find {prefix} -mindepth 1 -maxdepth 1 -type d ! -name '*.xxh3' {name}", warn=True)
            assert res['exitcode'] == 0 or 'No such file or directory' in res['stderr']
            dirs = res['stdout']
            xs = files.splitlines() + [x + '/' for x in dirs.splitlines()]
            if not _prefix.endswith('/'):
                _prefix = os.path.dirname(_prefix) + '/'
            xs = [x.split(_prefix)[-1] for x in xs]
    except subprocess.CalledProcessError:
        xs = []
    return {'status': 200,
            'body': json.dumps(xs)}

async def delete_handler(req):
    prefix = req['query']['prefix'].split('s4://')[-1]
    recursive = req['query'].get('recursive') == 'true'
    prefix = os.path.join(path_prefix, prefix)
    if recursive:
        prefix += '*'
    await submit(shell.run, 'rm -rf', prefix, prefix + '.xxh3')
    return {'status': 200}

async def return_port_handler(req):
    return_port(int(req['body']))
    return {'status': 200}

async def new_port_handler(req):
    if len(jobs) > max_jobs:
        return {'status': 429}
    else:
        return {'status': 200, 'body': str(new_port())}

async def health(req):
    return {'status': 200}

def submit(f, *a, executor=None, **kw):
    return tornado.ioloop.IOLoop.current().run_in_executor(executor, lambda: f(*a, **kw))

async def proc_garbage_collector():
    try:
        for k, v in list(jobs.items()):
            if time.monotonic() - v['time'] > s4.timeout:
                if v.get('temp_path'):
                    with util.exceptions.ignore(FileNotFoundError):
                        await submit(os.remove, v['temp_path'])
                    with util.exceptions.ignore(FileNotFoundError):
                        await submit(os.remove, v['temp_path'] + '.xxh3')
                del jobs[k]
    except:
        logging.exception('proc garbage collector died')
        time.sleep(1)
        os._exit(1)
    else:
        await tornado.gen.sleep(5)
        tornado.ioloop.IOLoop.current().add_callback(proc_garbage_collector)

def start(debug=False):
    util.log.setup()
    routes = [('/prepare_put', {'post': prepare_put_handler}),
              ('/confirm_put', {'post': confirm_put_handler}),
              ('/prepare_get', {'post': prepare_get_handler}),
              ('/confirm_get', {'post': confirm_get_handler}),
              ('/delete',      {'post': delete_handler}),
              ('/list',        {'get':  list_handler}),
              ('/new_port',    {'post': new_port_handler}),
              ('/return_port', {'post': return_port_handler}),
              ('/health',      {'get':  health})]
    logging.info(f'starting s4 server on port: {s4.http_port()}')
    web.app(routes, debug=debug).listen(s4.http_port())
    tornado.ioloop.IOLoop.current().add_callback(proc_garbage_collector)
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        sys.exit(1)

if __name__ == '__main__':
    if util.hacks.override('--debug'):
        shell.set_stream().__enter__()
    argh.dispatch_command(start)
