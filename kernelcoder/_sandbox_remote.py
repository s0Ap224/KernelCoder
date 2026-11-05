"""Small trusted supervisor copied to E2B, executed as root with Python -I.

Only stdlib imports: this file also runs independently in the default template.
User commands run under a dedicated UID with no privilege escalation. Keeping
control files outside /workspace prevents commands from forging sync results.
"""

import ctypes
import fnmatch
import hashlib
import json
import os
import pwd
import resource
import selectors
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

CONTROL = Path('/root/.kernelcoder-runner')
TREE = Path('/workspace')
USER = 'kernelcoder'


def save(path, value):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(value), encoding='utf-8')
    os.replace(temp, path)


def setup():
    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        subprocess.run(['/usr/sbin/useradd', '-m', '-s', '/bin/bash', USER], check=True)
        account = pwd.getpwnam(USER)
    TREE.mkdir(exist_ok=True)
    os.chown(TREE, account.pw_uid, account.pw_gid)
    CONTROL.mkdir(mode=0o700, exist_ok=True)


def workers():
    uid = pwd.getpwnam(USER).pw_uid
    found = []
    for proc in Path('/proc').iterdir():
        if not proc.name.isdigit():
            continue
        try:
            text = (proc / 'status').read_text()
            if int(text.split('Uid:')[1].split()[0]) == uid and '\nState:\tZ' not in text:
                found.append(int(proc.name))
        except (FileNotFoundError, ProcessLookupError):
            pass
    return found


def stop():
    for _ in range(50):
        running = workers()
        if not running:
            return
        for pid in running:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        time.sleep(.02)
    raise RuntimeError('Cannot confirm user processes stopped')


def path(name):
    parts = Path(name).parts
    if not parts or Path(name).is_absolute() or '..' in parts:
        raise ValueError('Invalid relative path')
    p = TREE
    if p.is_symlink():
        raise ValueError('Linked workspace')
    for part in parts:
        p = p / part
        if p.is_symlink():
            raise ValueError('Linked path')
    return p


def scan(config):
    if workers():
        raise RuntimeError('User processes still running')
    files, count, total = {}, 0, 0
    stack = [TREE]
    while stack:
        folder = stack.pop()
        for p in sorted(folder.iterdir()):
            count += 1
            if count > config['max_files']:
                raise ValueError('Workspace entry limit exceeded')
            name = p.relative_to(TREE).as_posix()
            if any(fnmatch.fnmatchcase(part.lower(), pat.lower()) or fnmatch.fnmatchcase(name.lower(), pat.lower())
                   for part in Path(name).parts for pat in config['excludes']):
                continue
            info = p.lstat()
            if stat.S_ISDIR(info.st_mode):
                stack.append(p)
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Link or special file refused: ' + name)
            if info.st_size > config['max_file']:
                raise ValueError('Oversized file: ' + name)
            data = p.read_bytes()
            total += len(data)
            if total > config['max_tree'] or len(data) > config['max_file']:
                raise ValueError('Workspace size limit exceeded')
            files[name] = {'sha256': hashlib.sha256(data).hexdigest(),
                           'mode': stat.S_IMODE(info.st_mode) & 0o777, 'size': len(data)}
    return files


def push(job, request):
    current = scan(request)
    desired = request['files']
    for name in sorted(current.keys() - desired.keys(), key=len, reverse=True):
        p = path(name)
        p.unlink()
        parent = p.parent
        while parent != TREE:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    account = pwd.getpwnam(USER)
    for name, entry in desired.items():
        if current.get(name) == entry:
            continue
        p = path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        # Project directories must be writable by the command's UID.
        for parent in p.parents:
            if parent == TREE.parent:
                break
            os.chown(parent, account.pw_uid, account.pw_gid)
        if p.is_dir():
            p.rmdir()  # A nonempty/excluded directory must never be erased.
        source = job / 'upload' / name
        data = source.read_bytes()
        if len(data) != entry['size'] or hashlib.sha256(data).hexdigest() != entry['sha256']:
            raise ValueError('Upload hash mismatch')
        temp = p.with_name(p.name + '.kernelcoder-upload')
        # Never follow a user-created temporary file/link.
        with temp.open('xb') as f:
            f.write(data)
        temp.chmod(entry['mode'])
        os.chown(temp, account.pw_uid, account.pw_gid)
        os.replace(temp, p)
    cwd = path(request['cwd']) if request['cwd'] != '.' else TREE
    cwd.mkdir(parents=True, exist_ok=True)
    for parent in (cwd, *cwd.parents):
        if parent == TREE.parent:
            break
        os.chown(parent, account.pw_uid, account.pw_gid)


class Output:
    def __init__(self):
        self.head = b''
        self.tail = b''
        self.total = 0

    def add(self, data):
        self.total += len(data)
        needed = 6000 - len(self.head)
        self.head += data[:needed]
        self.tail = (self.tail + data[needed:])[-3000:]

    def text(self):
        gap = f'\n... truncated ({self.total} bytes) ...\n'.encode() if self.total > 9000 else b''
        return (self.head + gap + self.tail).decode('utf-8', 'replace')


def execute(job, request):
    stop()
    account = pwd.getpwnam(USER)
    cwd = path(request['cwd']) if request['cwd'] != '.' else TREE
    if not cwd.is_dir():
        raise ValueError('Working directory does not exist')

    def demote():
        # setuid/sudo binaries cannot regain root after exec.
        if ctypes.CDLL(None).prctl(38, 1, 0, 0, 0) != 0:
            raise RuntimeError('Cannot set no_new_privs')
        resource.setrlimit(resource.RLIMIT_NPROC, (128, 128))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        os.setgroups([])
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)

    env = {'HOME': account.pw_dir, 'USER': USER, 'LOGNAME': USER,
           'PATH': f'{account.pw_dir}/.local/bin:/usr/local/bin:/usr/bin:/bin',
           'LANG': 'C.UTF-8', 'PYTHONUNBUFFERED': '1'}
    out, err = Output(), Output()
    status = 'completed'
    proc = subprocess.Popen(['/bin/bash', '-c', request['command']], cwd=cwd, env=env,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True, preexec_fn=demote)  # noqa: PLW1509 -- standalone single-threaded supervisor
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ, out)
    selector.register(proc.stderr, selectors.EVENT_READ, err)
    deadline = time.monotonic() + request['timeout']
    try:
        while proc.poll() is None or selector.get_map():
            if (job / 'cancel').exists() or time.monotonic() >= deadline:
                status = 'cancelled' if (job / 'cancel').exists() else 'timeout'
                stop()
            if proc.poll() is not None:
                stop()  # Also terminate detached/background writers.
            for key, _ in selector.select(.05):
                data = os.read(key.fileobj.fileno(), 8192)
                if data:
                    key.data.add(data)
                else:
                    selector.unregister(key.fileobj)
        proc.wait()
    finally:
        stop()
        selector.close()
        proc.stdout.close()
        proc.stderr.close()
    save(job / 'result.json', {'status': status, 'exit_code': proc.returncode,
                              'output': out.text() + ('\n[stderr]\n' + err.text() if err.total else ''),
                              'detail': 'Remote processes stopped; background jobs are not retained.'})


def main():
    action = sys.argv[1]
    if action == 'setup':
        setup()
        return
    job = CONTROL / sys.argv[2]
    request = json.loads((job / 'request.json').read_text())
    if action == 'push':
        push(job, request)
    elif action == 'run':
        execute(job, request)
    elif action == 'snapshot':
        save(job / 'manifest.json', scan(request))
    elif action == 'stop':
        stop()
    elif action == 'prune':
        shutil.rmtree(job)
    else:
        raise ValueError('Unknown action')


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:  # noqa: BLE001 -- remote process boundary, report a bounded diagnostic
        # Avoid returning arbitrary command text or huge filenames via SDK output.
        print(type(exc).__name__ + ': ' + str(exc)[:500], file=sys.stderr)
        sys.exit(1)
