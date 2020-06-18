#!/usr/bin/env pypy3
import shutil
import stat
import argh
import concurrent.futures
import json
import logging
import os
import s4
import sys
import tempfile
import time
import tornado.gen
import tornado.ioloop
import tornado.util
import tornado.concurrent
import util.log
import util.misc
import util.strings
import uuid
import web
import datetime

max_timeout = s4.timeout * 2 + 15 # one timeout for fifo queue, one timeout for the job once it starts, + grace period

io_jobs = {}

# setup pool sizes
num_cpus = os.cpu_count() or 1
max_io_jobs  = int(os.environ.get('S4_IO_JOBS',  num_cpus * 4))
max_cpu_jobs = int(os.environ.get('S4_CPU_JOBS', num_cpus))

# setup pools
io_pool   = concurrent.futures.ThreadPoolExecutor(max_io_jobs)
cpu_pool  = concurrent.futures.ThreadPoolExecutor(max_cpu_jobs)
find_pool = concurrent.futures.ThreadPoolExecutor(max_cpu_jobs)
solo_pool = concurrent.futures.ThreadPoolExecutor(1)

# pool submit fns
submit_io   = lambda f, *a, **kw: tornado.ioloop.IOLoop.current().run_in_executor(io_pool,   lambda: f(*a, **kw)) # type: ignore # noqa
submit_cpu  = lambda f, *a, **kw: tornado.ioloop.IOLoop.current().run_in_executor(cpu_pool,  lambda: f(*a, **kw)) # type: ignore # noqa
submit_find = lambda f, *a, **kw: tornado.ioloop.IOLoop.current().run_in_executor(find_pool, lambda: f(*a, **kw)) # type: ignore # noqa
submit_solo = lambda f, *a, **kw: tornado.ioloop.IOLoop.current().run_in_executor(solo_pool, lambda: f(*a, **kw)) # type: ignore # noqa

printf = "-printf '%TY-%Tm-%Td %TH:%TM:%TS %s %p\n'"

def new_uuid():
    for _ in range(10):
        val = str(uuid.uuid4())
        if val not in io_jobs:
            io_jobs[val] = {'start': time.monotonic()}
            return val
    assert False

def checksum_write(path, checksum):
    with open(checksum_path(path), 'w') as f:
        f.write(checksum)

def checksum_read(path):
    with open(checksum_path(path)) as f:
        return f.read()

def checksum_path(path):
    assert not path.endswith('/')
    return f'{path}.xxh3'

def exists(path):
    return os.path.isfile(path) and os.path.isfile(checksum_path(path))

def prepare_put(path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    assert not os.path.isfile(path)
    assert not os.path.isfile(checksum_path(path))
    open(checksum_path(path), 'w').close()
    port = util.net.free_port()
    return s4.new_temp_path('tempfiles'), port

def start(func, timeout):
    future = tornado.concurrent.Future()
    add_callback = tornado.ioloop.IOLoop.current().add_callback
    def fn(*a, **kw):
        add_callback(future.set_result, None)
        return func(*a, **kw)
    future = tornado.gen.with_timeout(datetime.timedelta(seconds=timeout), future)
    return future, fn

async def prepare_put_handler(request):
    key = request['query']['key']
    assert ' ' not in key
    assert s4.on_this_server(key)
    path = key.split('s4://')[-1]
    try:
        temp_path, port = await submit_solo(prepare_put, path)
    except AssertionError:
        return {'code': 409}
    else:
        try:
            started, s4_run = start(s4.run, s4.timeout)
            uuid = new_uuid()
            assert not os.path.isfile(temp_path)
            io_jobs[uuid] = {'start': time.monotonic(),
                             'future': submit_io(s4_run, f'recv {port} | xxh3 --stream > {temp_path}'),
                             'temp_path': temp_path,
                             'path': path}
            try:
                await started
            except tornado.util.TimeoutError:
                job = io_jobs.pop(uuid)
                job['future'].cancel()
                await submit_solo(s4.delete, checksum_path(path), temp_path)
                return {'code': 429}
            else:
                return {'code': 200, 'body': json.dumps([uuid, port])}
        except:
            s4.delete(checksum_path(path))
            raise

def confirm_put(path, temp_path, server_checksum):
    try:
        checksum_write(path, server_checksum)
        os.rename(temp_path, path)
        os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)
    except:
        s4.delete(path, temp_path, checksum_path(path))
        raise

async def confirm_put_handler(request):
    try:
        uuid = request['query']['uuid']
        client_checksum = request['query']['checksum']
        job = io_jobs.pop(uuid)
        result = await job['future']
        assert result['exitcode'] == 0, result
        server_checksum = result['stderr']
        assert client_checksum == server_checksum, [client_checksum, server_checksum, result]
        await submit_solo(confirm_put, job['path'], job['temp_path'], server_checksum)
        return {'code': 200}
    except:
        s4.delete(job['path'], job['temp_path'], checksum_path(job['path']))
        raise

async def prepare_get_handler(request):
    key = request['query']['key']
    port = request['query']['port']
    remote = request['remote']
    assert s4.on_this_server(key)
    path = key.split('s4://')[-1]
    if not await submit_solo(exists, path):
        return {'code': 404}
    else:
        started, s4_run = start(s4.run, s4.timeout)
        uuid = new_uuid()
        io_jobs[uuid] = {'start': time.monotonic(),
                         'future': submit_io(s4_run, f'< {path} xxh3 --stream | send {remote} {port}'),
                         'disk_checksum': await submit_solo(checksum_read, path)}
        try:
            await started
        except tornado.util.TimeoutError:
            io_jobs.pop(uuid)['future'].cancel()
            return {'code': 429}
        else:
            return {'code': 200, 'body': uuid}

async def confirm_get_handler(request):
    uuid = request['query']['uuid']
    client_checksum = request['query']['checksum']
    job = io_jobs.pop(uuid)
    result = await job['future']
    assert result['exitcode'] == 0, result
    server_checksum = result['stderr']
    assert job['disk_checksum'] == client_checksum == server_checksum, [job['disk_checksum'], client_checksum, server_checksum]
    return {'code': 200}

async def list_handler(request):
    prefix = request['query']['prefix']
    assert prefix.startswith('s4://')
    _prefix = prefix = prefix.split('s4://')[-1]
    if not _prefix.endswith('/'):
        _prefix = os.path.dirname(_prefix) + '/'
    recursive = request['query'].get('recursive') == 'true'
    if recursive:
        if not prefix.endswith('/'):
            prefix += '*'
        result = await submit_find(s4.run, f"find {prefix} -type f ! -name '*.xxh3' {printf}")
        assert result['exitcode'] == 0 or 'No such file or directory' in result['stderr'], result
        xs = [x.split() for x in result['stdout'].splitlines()]
        xs = [[date, time.split('.')[0], size, '/'.join(path.split('/')[1:])] for date, time, size, path in xs]
    else:
        name = ''
        if not prefix.endswith('/'):
            name = os.path.basename(prefix)
            name = f"-name '{name}*'"
            prefix = os.path.dirname(prefix)
        result = await submit_find(s4.run, f"find {prefix} -maxdepth 1 -type f ! -name '*.xxh3' {name} {printf}")
        assert result['exitcode'] == 0 or 'No such file or directory' in result['stderr'], result
        files = result['stdout']
        result = await submit_find(s4.run, f"find {prefix} -mindepth 1 -maxdepth 1 -type d ! -name '*.xxh3' {name}")
        assert result['exitcode'] == 0 or 'No such file or directory' in result['stderr'], result
        files = [x.split() for x in files.splitlines() if x.split()[-1].strip()]
        dirs = [('', '', 'PRE', x + '/') for x in result['stdout'].splitlines() if x.strip()]
        xs = [[date, time.split(".")[0], size, path.split(_prefix)[-1]] for date, time, size, path in files + dirs]
    return {'code': 200, 'body': json.dumps(xs)}

async def delete_handler(request):
    prefix = request['query']['prefix']
    assert prefix.startswith('s4://')
    prefix = prefix.split('s4://')[-1]
    recursive = request['query'].get('recursive') == 'true'
    if recursive:
        result = await submit_solo(s4.run, 'rm -rf', prefix + '*')
    else:
        result = await submit_solo(s4.run, 'rm -f', prefix, checksum_path(prefix))
    assert result['exitcode'] == 0, result
    return {'code': 200}

async def health_handler(request):
    return {'code': 200}

async def map_to_n_handler(request):
    inkey = request['query']['inkey']
    outdir = request['query']['outdir']
    assert s4.on_this_server(inkey)
    assert outdir.startswith('s4://') and outdir.endswith('/')
    inpath = os.path.abspath(inkey.split('s4://')[-1])
    cmd = util.strings.b64_decode(request['query']['b64cmd'])
    tempdir, result = await submit_cpu(run_in_persisted_tempdir, f'< {inpath} {cmd}')
    try:
        if result['exitcode'] != 0:
            return {'code': 400, 'body': json.dumps(result)}
        else:
            temp_paths = result['stdout'].splitlines()
            await submit_io(confirm_to_n, inpath, outdir, tempdir, temp_paths)
            return {'code': 200}
    finally:
        result = await submit_solo(s4.run, 'rm -rf', tempdir)
        assert result['exitcode'] == 0, result

def confirm_to_n(inpath, outdir, tempdir, temp_paths):
    for temp_path in temp_paths:
        temp_path = os.path.join(tempdir, temp_path)
        outkey = os.path.join(outdir, os.path.basename(inpath), os.path.basename(temp_path))
        result = s4.run(f's4 cp {temp_path} {outkey}')
        assert result['exitcode'] == 0, result
        s4.delete(temp_path)

async def map_from_n_handler(request):
    outdir = request['query']['outdir']
    assert outdir.startswith('s4://') and outdir.endswith('/')
    inkeys = json.loads(request['body'])
    assert all(s4.on_this_server(key) for key in inkeys)
    bucket_num = inkeys[0].split('/')[-1]
    outpath = os.path.join(outdir, bucket_num)
    cmd = util.strings.b64_decode(request['query']['b64cmd'])
    inpaths = [os.path.abspath(inkey.split('s4://')[-1]) for inkey in inkeys]
    result = await submit_cpu(run_in_tempdir, f'{cmd} | s4 cp - {outpath}', stdin='\n'.join(inpaths) + '\n')
    if result['exitcode'] != 0:
        return {'code': 400, 'body': json.dumps(result)}
    else:
        return {'code': 200}

def run_in_tempdir(*a, **kw):
    tempdir = tempfile.mkdtemp(dir='tempdirs')
    try:
        return s4.run(f'cd {tempdir};', *a, **kw)
    finally:
        shutil.rmtree(tempdir)

def run_in_persisted_tempdir(*a, **kw):
    tempdir = tempfile.mkdtemp(dir='tempdirs')
    return tempdir, s4.run(f'cd {tempdir};', *a, **kw)

async def map_handler(request):
    inkey = request['query']['inkey']
    outkey = request['query']['outkey']
    assert s4.on_this_server(inkey)
    assert s4.on_this_server(outkey)
    inpath = os.path.abspath(inkey.split('s4://')[-1])
    cmd = util.strings.b64_decode(request['query']['b64cmd'])
    result = await submit_cpu(run_in_tempdir, f'< {inpath} {cmd} | s4 cp - {outkey}')
    if result['exitcode'] != 0:
        return {'code': 400, 'body': json.dumps(result)}
    else:
        return {'code': 200}

@util.misc.exceptions_kill_pid
async def gc_jobs():
    for uid, job in list(io_jobs.items()):
        if job and time.monotonic() - job['start'] > max_timeout:
            with util.exceptions.ignore(KeyError):
                await submit_solo(s4.delete, job['temp_path'])
            io_jobs.pop(uid, None)
    await tornado.gen.sleep(5)
    tornado.ioloop.IOLoop.current().add_callback(gc_jobs)

def main(debug=False):
    util.log.setup(format='%(message)s')
    if not os.path.basename(os.getcwd()) == 's4_data':
        os.makedirs('s4_data/tempfiles', exist_ok=True)
        os.makedirs('s4_data/tempdirs',  exist_ok=True)
        os.chdir('s4_data')
    os.environ['LC_ALL'] = 'C'
    routes = [('/prepare_put', {'post': prepare_put_handler}),
              ('/confirm_put', {'post': confirm_put_handler}),
              ('/prepare_get', {'post': prepare_get_handler}),
              ('/confirm_get', {'post': confirm_get_handler}),
              ('/delete',      {'post': delete_handler}),
              ('/map',         {'post': map_handler}),
              ('/map_to_n',    {'post': map_to_n_handler}),
              ('/map_from_n',  {'post': map_from_n_handler}),
              ('/list',        {'get':  list_handler}),
              ('/health',      {'get':  health_handler})]
    port = s4.http_port()
    logging.info(f'starting s4 server on port: {port}')
    web.app(routes, debug=debug).listen(port, idle_connection_timeout=max_timeout, body_timeout=max_timeout)
    tornado.ioloop.IOLoop.current().add_callback(gc_jobs)
    try:
        tornado.ioloop.IOLoop.current().start()
    except KeyboardInterrupt:
        sys.exit(1)

if __name__ == '__main__':
    argh.dispatch_command(main)
