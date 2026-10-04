"""VeBusConfig - read, store and write VE.Bus configurations through a Victron GX.

Window tool for the PC (where VEConfigure runs). Talks to the GX over SSH:
- lists a configuration store on the GX (default /data/vebusconfig)
- reads the configuration from the VE.Bus system into the store (mk2vsc -r)
- writes a stored configuration to the VE.Bus system (mk2vsc -w), with an
  automatic backup before and a read-back comparison after
- downloads / uploads files between the store and a folder on the PC

The VE.Bus D-Bus service is detected on connect (dbus -y); if there are several,
the user chooses. Settings live next to this script in VeBusConfig.json; host
keys in VeBusConfig_known_hosts; actions are logged to VeBusConfig_log.md.
The password is entered at runtime only and never stored or logged.

See README.md.
"""
import base64
import datetime
import difflib
import hashlib
import json
import os
import queue
import re
import shlex
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import paramiko

HERE = Path(__file__).resolve().parent
CONF_FILE = HERE / 'VeBusConfig.json'
KNOWN_HOSTS = HERE / 'VeBusConfig_known_hosts'
LOG_FILE = HERE / 'VeBusConfig_log.md'

DEFAULTS = {
    'host': '',
    'port': 22,
    'user': 'root',
    'key_file': '',
    'remote_dir': '/data/vebusconfig',
    'local_dir': 'configs',
    # empty: detect on connect
    'vebus_service': '',
    # empty: /service/mk2-dbus.<port of the vebus service>
    'mk2_service': '',
    'mk2vsc': '/opt/victronenergy/mk2vsc/mk2vsc',
}

# mk2-dbus restarts after every mk2vsc run; the tunnel is only usable once it
# has been running for a while
MIN_SERVICE_UPTIME = 60
SERVICE_WAIT_MAX = 300
# writing a configuration can restart a GX that is built into the inverter,
# including its network, for minutes; wait and reconnect patiently
MK2VSC_TIMEOUT = 600
RECONNECT_TRIES = 60
RECONNECT_DELAY = 10
POLL = 3

CONFIG_RE = re.compile(r'^[A-Za-z0-9._-]+\.rvsc$')
FILE_RE = re.compile(r'^[A-Za-z0-9._-]+$')
REMOTE_PATH_RE = re.compile(r'^/[A-Za-z0-9._/-]+$')
SERVICE_RE = re.compile(r'com\.victronenergy\.vebus\.[A-Za-z0-9_]+')
PROTECTED_PREFIX = 'original_'
# differences in the last bytes are the file trailer / checksum
TAIL_BYTES = 16

LOG_HEADER = """# VeBusConfig log

Written by `VeBusConfig.py`. Append only, no addresses.

| Time | Action | File | Result |
|------|--------|------|--------|
"""


def now_stamp():
    return datetime.datetime.now().strftime('%Y-%m-%d_%H%M%S')


def log(action, name, result):
    if not LOG_FILE.exists():
        LOG_FILE.write_text(LOG_HEADER, encoding='utf-8')
    stamp = datetime.datetime.now().isoformat(timespec='seconds')
    with LOG_FILE.open('a', encoding='utf-8') as f:
        f.write('| {} | {} | `{}` | {} |\n'.format(stamp, action, name, result.replace('|', '/')))


def load_conf():
    conf = dict(DEFAULTS)
    if CONF_FILE.exists():
        conf.update(json.loads(CONF_FILE.read_text(encoding='utf-8-sig')))
    return conf


def save_conf(conf):
    # only known keys, never the password
    data = {k: conf.get(k, v) for k, v in DEFAULTS.items()}
    CONF_FILE.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')


def resolve_local(value):
    """Local folder: relative paths are relative to this script."""
    p = Path(value)
    return p if p.is_absolute() else (HERE / p).resolve()


def store_local(path):
    """Keep paths on the same drive relative to this script (no drive letters)."""
    try:
        return Path(os.path.relpath(Path(path).resolve(), HERE)).as_posix()
    except ValueError:
        return str(path)


def check_remote_path(value, what):
    """Returns an error text or None."""
    if not REMOTE_PATH_RE.match(value) or value.rstrip('/') == '' or '..' in value.split('/'):
        return '{} must be an absolute path with letters, digits, . _ - / only'.format(what)
    return None


def compare(written, readback):
    """Differences between two config files, ignoring the trailer.

    Returns a list of (offset_written, length_written, offset_readback, length_readback).
    """
    sm = difflib.SequenceMatcher(None, written, readback, autojunk=False)
    out = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == 'equal':
            continue
        if i1 >= len(written) - TAIL_BYTES and j1 >= len(readback) - TAIL_BYTES:
            continue
        out.append((i1, i2 - i1, j1, j2 - j1))
    return out


def parse_uptime(svstat_output):
    m = re.search(rb': up \(pid \d+\) (\d+) seconds', svstat_output)
    return int(m.group(1)) if m else None


def parse_services(output):
    return sorted(set(SERVICE_RE.findall(output.decode('utf-8', 'replace'))))


def parse_listing(output):
    files = []
    for line in output.decode('utf-8', 'replace').splitlines():
        parts = line.split('|')
        if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
            continue
        name = parts[0].rsplit('/', 1)[-1]
        if name.startswith('.'):
            continue
        files.append((name, int(parts[1]), int(parts[2])))
    return sorted(files)


class AskHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    """Ask the user before trusting an unknown host key, then remember it."""

    def __init__(self, parent):
        self.parent = parent

    def missing_host_key(self, client, hostname, key):
        fp = base64.b64encode(hashlib.sha256(key.asbytes()).digest()).decode().rstrip('=')
        ok = messagebox.askyesno(
            'Unknown host key',
            'The GX presents a host key that is not known yet:\n\n'
            '{} SHA256:{}\n\n'
            'Compare it on the GX with\n'
            'ssh-keygen -lf /etc/ssh/ssh_host_{}_key.pub\n\n'
            'Trust this key?'.format(key.get_name(), fp, key.get_name().split('-')[-1]),
            parent=self.parent)
        if not ok:
            raise paramiko.SSHException('host key rejected by user')
        client.get_host_keys().add(hostname, key.get_name(), key)
        client.save_host_keys(str(KNOWN_HOSTS))


class Gx:
    """SSH access to the GX. Interactive connect in the GUI thread only."""

    def __init__(self, conf):
        self.conf = conf
        self.client = None
        self.password = None
        self.remote_dir = conf['remote_dir']
        self.mk2vsc_path = conf['mk2vsc']
        self.service = None
        self.svc_dir = None
        self.tunnel = None

    def _kwargs(self):
        kwargs = {
            'hostname': self.conf['host'],
            'port': int(self.conf.get('port', 22)),
            'username': self.conf.get('user', 'root'),
            'timeout': 10,
            'allow_agent': False,
            'look_for_keys': False,
        }
        key_file = self.conf.get('key_file')
        if key_file:
            kwargs['key_filename'] = str(resolve_local(key_file))
        if self.password is not None:
            kwargs['password'] = self.password
        return kwargs

    def _new_client(self, parent=None):
        client = paramiko.SSHClient()
        if KNOWN_HOSTS.exists():
            client.load_host_keys(str(KNOWN_HOSTS))
        if parent is None:
            client.set_missing_host_key_policy(paramiko.RejectPolicy())
        else:
            client.set_missing_host_key_policy(AskHostKeyPolicy(parent))
        return client

    def connect(self, parent):
        # raises paramiko.AuthenticationException on a wrong password
        client = self._new_client(parent)
        client.connect(**self._kwargs())
        self.client = client

    def reconnect(self):
        # non-interactive: known host key and stored password only
        client = self._new_client()
        client.connect(**self._kwargs())
        self.client = client

    def run(self, cmd, data=None, timeout=60):
        stdin, stdout, stderr = self.client.exec_command(cmd, timeout=timeout)
        if data is not None:
            stdin.write(data)
            stdin.channel.shutdown_write()
        out = stdout.read()
        err = stderr.read()
        return stdout.channel.recv_exit_status(), out, err

    def run_retry(self, cmd, progress, tries=RECONNECT_TRIES):
        for attempt in range(tries):
            try:
                return self.run(cmd)
            except (paramiko.SSHException, OSError, EOFError):
                progress('Connection lost, reconnecting ({}/{})'.format(attempt + 1, tries))
                time.sleep(RECONNECT_DELAY)
                try:
                    self.reconnect()
                except (paramiko.SSHException, OSError, EOFError):
                    pass
        raise RuntimeError('connection to the GX lost')

    def start_detached(self, cmd, progress):
        """Start a command without waiting for it (the channel is closed right away)."""
        for attempt in range(RECONNECT_TRIES):
            try:
                chan = self.client.get_transport().open_session()
                chan.exec_command(cmd)
                time.sleep(1)
                chan.close()
                return
            except (paramiko.SSHException, OSError, EOFError, AttributeError):
                progress('Connection lost, reconnecting ({}/{})'.format(attempt + 1, RECONNECT_TRIES))
                time.sleep(RECONNECT_DELAY)
                try:
                    self.reconnect()
                except (paramiko.SSHException, OSError, EOFError):
                    pass
        raise RuntimeError('connection to the GX lost')

    # --- VE.Bus service ---------------------------------------------------------

    def find_services(self):
        _, out, _ = self.run('dbus -y 2>/dev/null; true')
        return parse_services(out)

    def use_service(self, service):
        if not SERVICE_RE.fullmatch(service):
            raise RuntimeError('invalid VE.Bus service name')
        self.service = service
        port = service.rsplit('.', 1)[-1]
        self.svc_dir = self.conf.get('mk2_service') or '/service/mk2-dbus.' + port
        self.tunnel = 'dbus://{}/Interfaces/Mk2/Tunnel'.format(service)

    # --- store ------------------------------------------------------------------

    def path(self, name):
        return shlex.quote(self.remote_dir + '/' + name)

    def list_store(self):
        cmd = ('mkdir -p {d} && cd {d} && '
               'for f in *; do [ -f "$f" ] && stat -c "%n|%s|%Y" "$f"; done; true').format(d=shlex.quote(self.remote_dir))
        _, out, _ = self.run(cmd)
        return parse_listing(out)

    def exists(self, name, progress=None):
        cmd = 'test -e ' + self.path(name)
        code, _, _ = self.run_retry(cmd, progress) if progress else self.run(cmd)
        return code == 0

    def get(self, name, progress=None):
        cmd = 'cat ' + self.path(name)
        code, out, _ = self.run_retry(cmd, progress) if progress else self.run(cmd)
        if code != 0:
            raise RuntimeError('cannot read ' + name)
        return out

    def put(self, name, data):
        p = self.path(name)
        code, _, _ = self.run('cat > {p}.part && mv {p}.part {p}'.format(p=p), data=data)
        if code != 0:
            raise RuntimeError('cannot write ' + name)

    # --- mk2vsc -------------------------------------------------------------------

    def service_uptime(self, progress):
        _, out, err = self.run_retry('svstat ' + shlex.quote(self.svc_dir) + ' 2>&1', progress)
        if b'unable' in out or b'unable' in err:
            raise RuntimeError('service directory {} not found; set "mk2_service" in VeBusConfig.json'.format(
                self.svc_dir))
        return parse_uptime(out)

    def wait_service(self, progress):
        start = time.time()
        while True:
            up = self.service_uptime(progress)
            if up is not None and up >= MIN_SERVICE_UPTIME:
                return
            if time.time() - start > SERVICE_WAIT_MAX:
                raise RuntimeError('mk2-dbus not ready after {} s'.format(SERVICE_WAIT_MAX))
            progress('Waiting for mk2-dbus: up {} s, need {} s'.format(up, MIN_SERVICE_UPTIME))
            time.sleep(5)

    def mk2vsc(self, mode, name, progress):
        """Run mk2vsc detached on the GX (survives SSH drops). Returns (exit code, output)."""
        d = shlex.quote(self.remote_dir)
        inner = '{m} {mode} -s {t} -f {p} > .mk2vsc.log 2>&1; echo $? > .mk2vsc.exit'.format(
            m=shlex.quote(self.mk2vsc_path), mode=mode, t=shlex.quote(self.tunnel), p=self.path(name))
        # clean up first and wait for it; then start detached. Separate steps on
        # purpose: 'a && b && nohup c &' would background the whole list in a
        # subshell that keeps the SSH channel open until mk2vsc has finished.
        self.run_retry('cd {d}; rm -f .mk2vsc.exit .mk2vsc.log'.format(d=d), progress)
        cmd = 'cd {d}; nohup sh -c {inner} < /dev/null > /dev/null 2>&1 &'.format(d=d, inner=shlex.quote(inner))
        self.start_detached(cmd, progress)
        start = time.time()
        while time.time() - start < MK2VSC_TIMEOUT:
            time.sleep(POLL)
            _, out, _ = self.run_retry('cat {}/.mk2vsc.exit 2>/dev/null; true'.format(d), progress)
            if out.strip():
                _, text, _ = self.run_retry('cat {}/.mk2vsc.log 2>/dev/null; true'.format(d), progress)
                return int(out.strip()), text.decode('utf-8', 'replace').strip()
            progress('mk2vsc {} running: {} s'.format(mode, int(time.time() - start)))
        raise RuntimeError('mk2vsc did not finish within {} s'.format(MK2VSC_TIMEOUT))

    def read_config(self, name, progress):
        if self.exists(name, progress):
            raise RuntimeError(name + ' already exists in the store')
        self.wait_service(progress)
        progress('Reading configuration from the VE.Bus system into ' + name)
        code, text = self.mk2vsc('-r', name, progress)
        if code != 0 or not self.exists(name, progress):
            raise RuntimeError('mk2vsc -r failed (exit {}): {}'.format(code, text))
        self.run_retry('cd {} && sha256sum {n} > {n}.sha256'.format(
            shlex.quote(self.remote_dir), n=shlex.quote(name)), progress)

    def write_config(self, name, progress, report):
        stamp = now_stamp()
        before = 'auto_before_{}.rvsc'.format(stamp)
        after = 'auto_after_{}.rvsc'.format(stamp)
        progress('Backup of the current configuration: ' + before)
        self.read_config(before, progress)
        log('backup', before, 'ok')
        self.wait_service(progress)
        progress('Writing {} to the VE.Bus system'.format(name))
        code, text = self.mk2vsc('-w', name, progress)
        if code != 0:
            raise RuntimeError('mk2vsc -w failed (exit {}): {}'.format(code, text))
        log('write', name, 'ok, backup ' + before)
        progress('Reading back for comparison: ' + after)
        self.read_config(after, progress)
        diffs = compare(self.get(name, progress), self.get(after, progress))
        if not diffs:
            report('Read-back identical to {} (trailer ignored).'.format(name))
            log('verify', after, 'identical')
        else:
            places = ', '.join('{}(+{})'.format(i, n) for i, n, _, _ in diffs)
            report('Read-back differs from {} outside the trailer in {} place(s): {}.\n'
                   'A single 2-byte difference is the running counter (expected).'.format(name, len(diffs), places))
            log('verify', after, '{} difference(s): {}'.format(len(diffs), places))


class App:
    def __init__(self, root):
        self.root = root
        self.gx = None
        self.busy = False
        self.messages = queue.Queue()
        root.title('VeBusConfig - VE.Bus configurations via GX')
        root.geometry('1000x660')
        self._build()
        if self.local_dir.is_dir():
            self.refresh_local()
        root.after(200, self._poll_messages)

    @property
    def local_dir(self):
        return resolve_local(self.local_var.get().strip())

    # --- layout -----------------------------------------------------------------

    def _build(self):
        conf = load_conf()
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill='x')
        self.host = tk.StringVar(value=conf['host'])
        self.password = tk.StringVar()
        ttk.Label(top, text='Host').pack(side='left')
        ttk.Entry(top, textvariable=self.host, width=18).pack(side='left', padx=4)
        ttk.Label(top, text='Password').pack(side='left')
        pw = ttk.Entry(top, textvariable=self.password, show='*', width=16)
        pw.pack(side='left', padx=4)
        pw.bind('<Return>', lambda _: self.connect())
        ttk.Button(top, text='Connect', command=self.connect).pack(side='left', padx=4)
        self.status = ttk.Label(top, text='GX: not connected')
        self.status.pack(side='left', padx=8)
        ttk.Button(top, text='Refresh', command=self.refresh_all).pack(side='right')

        self.remote_var = tk.StringVar(value=conf['remote_dir'])
        self.local_var = tk.StringVar(value=conf['local_dir'])

        lists = ttk.Frame(self.root, padding=6)
        lists.pack(fill='both', expand=True)
        lists.columnconfigure(0, weight=1)
        lists.columnconfigure(1, weight=1)
        lists.rowconfigure(1, weight=1)
        rh = ttk.Frame(lists)
        rh.grid(row=0, column=0, sticky='ew')
        ttk.Label(rh, text='GX store').pack(side='left')
        ttk.Entry(rh, textvariable=self.remote_var, width=30).pack(side='left', padx=4, fill='x', expand=True)
        lh = ttk.Frame(lists)
        lh.grid(row=0, column=1, sticky='ew')
        ttk.Label(lh, text='PC folder').pack(side='left')
        ttk.Entry(lh, textvariable=self.local_var, width=30).pack(side='left', padx=4, fill='x', expand=True)
        ttk.Button(lh, text='Browse...', command=self.browse_local).pack(side='left')
        self.remote = self._tree(lists, 0)
        self.local = self._tree(lists, 1)

        rb = ttk.Frame(lists)
        rb.grid(row=2, column=0, sticky='w', pady=4)
        self.buttons = [
            ttk.Button(rb, text='Read from VE.Bus...', command=self.read_config),
            ttk.Button(rb, text='Write to VE.Bus...', command=self.write_config),
            ttk.Button(rb, text='Download ->', command=self.download),
        ]
        lb = ttk.Frame(lists)
        lb.grid(row=2, column=1, sticky='w', pady=4)
        self.buttons += [
            ttk.Button(lb, text='<- Upload', command=self.upload),
            ttk.Button(lb, text='Open folder', command=lambda: os.startfile(self.local_dir)),
        ]
        for b in self.buttons:
            b.pack(side='left', padx=2)

        self.out = tk.Text(self.root, height=10, wrap='word')
        self.out.pack(fill='x', padx=6, pady=6)

    def _tree(self, parent, column):
        tree = ttk.Treeview(parent, columns=('size', 'modified'), selectmode='browse')
        tree.heading('#0', text='Name')
        tree.heading('size', text='Size')
        tree.heading('modified', text='Modified')
        tree.column('#0', width=260)
        tree.column('size', width=70, anchor='e')
        tree.column('modified', width=140)
        tree.grid(row=1, column=column, sticky='nsew', padx=2)
        return tree

    @staticmethod
    def _fill(tree, files):
        tree.delete(*tree.get_children())
        for name, size, mtime in files:
            when = datetime.datetime.fromtimestamp(mtime).strftime('%Y-%m-%d %H:%M:%S')
            tree.insert('', 'end', iid=name, text=name, values=(size, when))

    @staticmethod
    def _selected(tree):
        sel = tree.selection()
        return sel[0] if sel else None

    # --- messages and background work -------------------------------------------

    def say(self, text):
        self.messages.put(text)

    def _poll_messages(self):
        while not self.messages.empty():
            item = self.messages.get()
            if callable(item):
                item()
            else:
                stamp = datetime.datetime.now().strftime('%H:%M:%S')
                self.out.insert('end', '{} {}\n'.format(stamp, item))
                self.out.see('end')
        self.root.after(200, self._poll_messages)

    def _set_busy(self, busy):
        self.busy = busy
        for b in self.buttons[:4]:
            b.state(['disabled'] if busy else ['!disabled'])

    def background(self, title, work):
        if self.busy:
            return
        self._set_busy(True)
        self.say(title + ' ...')

        def run():
            try:
                work()
                self.say(title + ': done')
            except Exception as e:
                self.say('{}: FAILED - {}'.format(title, e))
            finally:
                self.messages.put(lambda: self._set_busy(False))
                self.messages.put(self.refresh_all)

        threading.Thread(target=run, daemon=True).start()

    def need_gx(self):
        if not (self.gx and self.gx.client and self.gx.service):
            messagebox.showinfo('VeBusConfig', 'Connect to the GX first.', parent=self.root)
            return False
        return self.apply_paths()

    def apply_paths(self):
        """Validate both paths, take them over and remember them. GUI thread only."""
        remote = self.remote_var.get().strip().rstrip('/')
        err = check_remote_path(remote, 'GX store')
        if err:
            messagebox.showerror('VeBusConfig', err, parent=self.root)
            return False
        local = self.local_dir
        if not local.is_dir():
            if not messagebox.askyesno('VeBusConfig', 'PC folder does not exist:\n{}\n\nCreate it?'.format(local),
                                       parent=self.root):
                return False
            local.mkdir(parents=True)
        if self.gx and self.gx.remote_dir != remote:
            if not remote.startswith('/data/'):
                messagebox.showwarning('VeBusConfig', 'GX store is not under /data and will not survive a '
                                       'firmware update of the GX.', parent=self.root)
            self.gx.remote_dir = remote
        self.remote_var.set(remote)
        self.local_var.set(store_local(local))
        conf = load_conf()
        conf.update({'host': self.host.get().strip() or conf['host'],
                     'remote_dir': remote, 'local_dir': self.local_var.get()})
        save_conf(conf)
        return True

    def browse_local(self):
        chosen = filedialog.askdirectory(initialdir=str(self.local_dir), parent=self.root)
        if chosen:
            self.local_var.set(store_local(chosen))
            self.refresh_all()

    # --- actions ----------------------------------------------------------------

    def _choose_service(self, conf):
        services = self.gx.find_services()
        wanted = conf.get('vebus_service')
        if wanted:
            if wanted not in services:
                raise RuntimeError('vebus_service from VeBusConfig.json not found on the GX')
            return wanted
        if not services:
            raise RuntimeError('no VE.Bus service found on the GX')
        if len(services) == 1:
            return services[0]
        text = '\n'.join('{}: {}'.format(i + 1, s) for i, s in enumerate(services))
        n = simpledialog.askinteger('VeBusConfig', 'Several VE.Bus systems found:\n\n{}\n\nUse number:'.format(text),
                                    minvalue=1, maxvalue=len(services), parent=self.root)
        if n is None:
            raise RuntimeError('no VE.Bus system chosen')
        return services[n - 1]

    def connect(self):
        if self.busy:
            return
        conf = load_conf()
        conf['host'] = self.host.get().strip()
        conf['remote_dir'] = self.remote_var.get().strip().rstrip('/')
        if not conf['host']:
            messagebox.showerror('VeBusConfig', 'Enter the host (address of the GX).', parent=self.root)
            return
        if not conf.get('key_file') and not self.password.get():
            messagebox.showerror('VeBusConfig', 'Enter the password of the GX.', parent=self.root)
            return
        for key, what in (('mk2vsc', 'mk2vsc path'), ('mk2_service', 'mk2_service')):
            if conf.get(key):
                err = check_remote_path(conf[key], what)
                if err:
                    messagebox.showerror('VeBusConfig', err + ' (VeBusConfig.json)', parent=self.root)
                    return
        self.gx = Gx(conf)
        self.gx.password = self.password.get() or None
        ok = False
        try:
            self.gx.connect(self.root)
            self.gx.use_service(self._choose_service(conf))
            ok = True
        except paramiko.AuthenticationException:
            self.say('Connect failed: wrong password or user')
        except RuntimeError as e:
            self.say('Connect failed: {}'.format(e))
        except Exception as e:
            # type only: messages may contain the address
            self.say('Connect failed: ' + type(e).__name__)
        if not ok:
            self.gx = None
            self.status.config(text='GX: not connected')
            return
        self.status.config(text='GX: connected   ' + self.gx.service)
        log('connect', self.gx.service, 'ok')
        self.refresh_all()

    def refresh_local(self):
        files = []
        if self.local_dir.is_dir():
            for p in sorted(self.local_dir.iterdir()):
                if p.is_file():
                    st = p.stat()
                    files.append((p.name, st.st_size, int(st.st_mtime)))
        self._fill(self.local, files)

    def refresh_all(self):
        if self.busy or not self.apply_paths():
            return
        self.refresh_local()
        if self.gx and self.gx.client and self.gx.service:
            try:
                self._fill(self.remote, self.gx.list_store())
                up = parse_uptime(self.gx.run('svstat ' + shlex.quote(self.gx.svc_dir))[1])
                self.status.config(text='GX: connected   {}   mk2-dbus up {} s'.format(self.gx.service, up))
            except Exception as e:
                self.say('Refresh failed: ' + type(e).__name__)

    def read_config(self):
        if not self.need_gx():
            return
        name = simpledialog.askstring('Read from VE.Bus', 'Name for the new file in the GX store:',
                                      initialvalue='read_{}.rvsc'.format(now_stamp()), parent=self.root)
        if not name:
            return
        if not CONFIG_RE.match(name):
            messagebox.showerror('VeBusConfig', 'Name must be letters, digits, . _ - and end in .rvsc',
                                 parent=self.root)
            return

        def work():
            self.gx.read_config(name, self.say)
            log('read', name, 'ok')

        self.background('Read ' + name, work)

    def write_config(self):
        if not self.need_gx():
            return
        name = self._selected(self.remote)
        if not name or not CONFIG_RE.match(name):
            messagebox.showinfo('VeBusConfig', 'Select a .rvsc file in the GX store.', parent=self.root)
            return
        if not messagebox.askyesno(
                'Write to VE.Bus',
                'Write "{}" to the VE.Bus system?\n\n'
                '- First the current configuration is saved as auto_before_....\n'
                '- The inverter/charger may restart; AC-out may be without power for a few seconds.\n'
                '- Afterwards the configuration is read back and compared.\n\n'
                'This takes several minutes. Continue?'.format(name),
                icon='warning', parent=self.root):
            return

        def work():
            self.gx.write_config(name, self.say, self.say)

        self.background('Write ' + name, work)

    def download(self):
        if not self.need_gx():
            return
        name = self._selected(self.remote)
        if not name or not FILE_RE.match(name):
            messagebox.showinfo('VeBusConfig', 'Select a file in the GX store.', parent=self.root)
            return
        target = self.local_dir / name
        if target.exists():
            if name.startswith(PROTECTED_PREFIX):
                messagebox.showerror('VeBusConfig', name + ' exists on the PC and is protected.', parent=self.root)
                return
            if not messagebox.askyesno('VeBusConfig', name + ' exists on the PC. Overwrite?', parent=self.root):
                return

        def work():
            data = self.gx.get(name)
            target.write_bytes(data)
            log('download', name, '{} bytes'.format(len(data)))

        self.background('Download ' + name, work)

    def upload(self):
        if not self.need_gx():
            return
        name = self._selected(self.local)
        if not name or not FILE_RE.match(name):
            messagebox.showinfo('VeBusConfig', 'Select a file on the PC (letters, digits, . _ - only).',
                                parent=self.root)
            return
        if name.startswith(PROTECTED_PREFIX) and self.gx.exists(name):
            messagebox.showerror('VeBusConfig', name + ' exists on the GX and is protected.', parent=self.root)
            return
        if self.gx.exists(name) and not messagebox.askyesno('VeBusConfig', name + ' exists on the GX. Overwrite?',
                                                            parent=self.root):
            return
        data = (self.local_dir / name).read_bytes()

        def work():
            self.gx.put(name, data)
            log('upload', name, '{} bytes'.format(len(data)))

        self.background('Upload ' + name, work)


def main():
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == '__main__':
    main()
