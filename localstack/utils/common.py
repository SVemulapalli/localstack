import io
import os
import re
import pwd
import grp
import sys
import json
import uuid
import time
import glob
import base64
import socket
import hashlib
import decimal
import logging
import tarfile
import zipfile
import binascii
import calendar
import tempfile
import threading
import subprocess
import six
import shutil
import requests
import dns.resolver
import functools
from io import BytesIO
from contextlib import closing
from datetime import datetime, date
from six import with_metaclass
from six.moves import cStringIO as StringIO
from six.moves.urllib.parse import urlparse
from multiprocessing.dummy import Pool
from localstack import config
from localstack.config import DEFAULT_ENCODING
from localstack.constants import ENV_DEV
from localstack.utils import bootstrap
from localstack.utils.bootstrap import FuncThread

# arrays for temporary files and resources
TMP_FILES = []
TMP_THREADS = []
TMP_PROCESSES = []

# cache clean variables
CACHE_CLEAN_TIMEOUT = 60 * 5
CACHE_MAX_AGE = 60 * 60
CACHE_FILE_PATTERN = os.path.join(tempfile.gettempdir(), '_random_dir_', 'cache.*.json')
last_cache_clean_time = {'time': 0}
mutex_clean = threading.Semaphore(1)

# misc. constants
TIMESTAMP_FORMAT = '%Y-%m-%dT%H:%M:%S'
TIMESTAMP_FORMAT_MICROS = '%Y-%m-%dT%H:%M:%S.%fZ'
CODEC_HANDLER_UNDERSCORE = 'underscore'

# chunk size for file downloads
DOWNLOAD_CHUNK_SIZE = 1024 * 1024

# set up logger
LOG = logging.getLogger(__name__)

# flag to indicate whether we've received and processed the stop signal
INFRA_STOPPED = False

# generic cache object
CACHE = {}

# lock for creating certificate files
SSL_CERT_LOCK = threading.RLock()


class CustomEncoder(json.JSONEncoder):
    """ Helper class to convert JSON documents with datetime, decimals, or bytes. """

    def default(self, o):
        if isinstance(o, decimal.Decimal):
            if o % 1 > 0:
                return float(o)
            else:
                return int(o)
        if isinstance(o, (datetime, date)):
            return str(o)
        if isinstance(o, six.binary_type):
            return to_str(o)
        try:
            return super(CustomEncoder, self).default(o)
        except Exception:
            return None


class ShellCommandThread(FuncThread):
    """ Helper class to run a shell command in a background thread. """

    def __init__(self, cmd, params={}, outfile=None, env_vars={}, stdin=False,
            quiet=True, inherit_cwd=False, inherit_env=True):
        self.cmd = cmd
        self.process = None
        self.outfile = outfile or os.devnull
        self.stdin = stdin
        self.env_vars = env_vars
        self.inherit_cwd = inherit_cwd
        self.inherit_env = inherit_env
        FuncThread.__init__(self, self.run_cmd, params, quiet=quiet)

    def run_cmd(self, params):

        def convert_line(line):
            line = to_str(line or '')
            return '%s\r\n' % line.strip()

        def filter_line(line):
            """ Return True if this line should be filtered, i.e., not printed """
            return '(Press CTRL+C to quit)' in line

        try:
            self.process = run(self.cmd, asynchronous=True, stdin=self.stdin, outfile=self.outfile,
                env_vars=self.env_vars, inherit_cwd=self.inherit_cwd, inherit_env=self.inherit_env)
            if self.outfile:
                if self.outfile == subprocess.PIPE:
                    # get stdout/stderr from child process and write to parent output
                    streams = ((self.process.stdout, sys.stdout), (self.process.stderr, sys.stderr))
                    for instream, outstream in streams:
                        for line in iter(instream.readline, None):
                            # `line` should contain a newline at the end as we're iterating,
                            # hence we can safely break the loop if `line` is None or empty string
                            if line in [None, '', b'']:
                                break
                            if not (line and line.strip()) and self.is_killed():
                                break
                            line = convert_line(line)
                            if filter_line(line):
                                continue
                            outstream.write(line)
                            outstream.flush()
                self.process.wait()
            else:
                self.process.communicate()
        except Exception as e:
            if self.process and not self.quiet:
                LOG.warning('Shell command error "%s": %s' % (e, self.cmd))
        if self.process and not self.quiet and self.process.returncode != 0:
            LOG.warning('Shell command exit code "%s": %s' % (self.process.returncode, self.cmd))

    def is_killed(self):
        if not self.process:
            return True
        if INFRA_STOPPED:
            return True
        # Note: Do NOT import "psutil" at the root scope, as this leads
        # to problems when importing this file from our test Lambdas in Docker
        # (Error: libc.musl-x86_64.so.1: cannot open shared object file)
        import psutil
        return not psutil.pid_exists(self.process.pid)

    def stop(self, quiet=False):
        # Note: Do NOT import "psutil" at the root scope, as this leads
        # to problems when importing this file from our test Lambdas in Docker
        # (Error: libc.musl-x86_64.so.1: cannot open shared object file)
        import psutil

        if getattr(self, 'stopped', False):
            return

        if not self.process:
            LOG.warning("No process found for command '%s'" % self.cmd)
            return

        parent_pid = self.process.pid
        try:
            parent = psutil.Process(parent_pid)
            for child in parent.children(recursive=True):
                child.kill()
            parent.kill()
            self.process = None
        except Exception:
            if not quiet:
                LOG.warning('Unable to kill process with pid %s' % parent_pid)
        self.stopped = True


class JsonObject(object):
    """ Generic JSON serializable object for simplified subclassing """

    def to_json(self, indent=None):
        return json.dumps(self,
            default=lambda o: ((float(o) if o % 1 > 0 else int(o))
                if isinstance(o, decimal.Decimal) else o.__dict__),
            sort_keys=True, indent=indent)

    def apply_json(self, j):
        if isinstance(j, str):
            j = json.loads(j)
        self.__dict__.update(j)

    def to_dict(self):
        return json.loads(self.to_json())

    @classmethod
    def from_json(cls, j):
        j = JsonObject.as_dict(j)
        result = cls()
        result.apply_json(j)
        return result

    @classmethod
    def from_json_list(cls, json_list):
        return [cls.from_json(j) for j in json_list]

    @classmethod
    def as_dict(cls, obj):
        if isinstance(obj, dict):
            return obj
        return obj.to_dict()

    def __str__(self):
        return self.to_json()

    def __repr__(self):
        return self.__str__()


class CaptureOutput(object):
    """ A context manager that captures stdout/stderr of the current thread. Use it as follows:

        with CaptureOutput() as c:
            ...
        print(c.stdout(), c.stderr())
    """

    orig_stdout = sys.stdout
    orig_stderr = sys.stderr
    orig___stdout = sys.__stdout__
    orig___stderr = sys.__stderr__
    CONTEXTS_BY_THREAD = {}

    class LogStreamIO(io.StringIO):
        def write(self, s):
            if isinstance(s, str) and hasattr(s, 'decode'):
                s = s.decode('unicode-escape')
            return super(CaptureOutput.LogStreamIO, self).write(s)

    def __init__(self):
        self._stdout = self.LogStreamIO()
        self._stderr = self.LogStreamIO()

    def __enter__(self):
        # Note: import werkzeug here (not at top of file) to allow dependency pruning
        from werkzeug.local import LocalProxy

        ident = self._ident()
        if ident not in self.CONTEXTS_BY_THREAD:
            self.CONTEXTS_BY_THREAD[ident] = self
            self._set(LocalProxy(self._proxy(sys.stdout, 'stdout')),
                      LocalProxy(self._proxy(sys.stderr, 'stderr')),
                      LocalProxy(self._proxy(sys.__stdout__, 'stdout')),
                      LocalProxy(self._proxy(sys.__stderr__, 'stderr')))
        return self

    def __exit__(self, type, value, traceback):
        ident = self._ident()
        removed = self.CONTEXTS_BY_THREAD.pop(ident, None)
        if not self.CONTEXTS_BY_THREAD:
            # reset pointers
            self._set(self.orig_stdout, self.orig_stderr, self.orig___stdout, self.orig___stderr)
        # get value from streams
        removed._stdout.flush()
        removed._stderr.flush()
        out = removed._stdout.getvalue()
        err = removed._stderr.getvalue()
        # close handles
        removed._stdout.close()
        removed._stderr.close()
        removed._stdout = out
        removed._stderr = err

    def _set(self, out, err, __out, __err):
        sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__ = (out, err, __out, __err)

    def _proxy(self, original_stream, type):
        def proxy():
            ident = self._ident()
            ctx = self.CONTEXTS_BY_THREAD.get(ident)
            if ctx:
                return ctx._stdout if type == 'stdout' else ctx._stderr
            return original_stream

        return proxy

    def _ident(self):
        return threading.currentThread().ident

    def stdout(self):
        return self._stream_value(self._stdout)

    def stderr(self):
        return self._stream_value(self._stderr)

    def _stream_value(self, stream):
        return stream.getvalue() if hasattr(stream, 'getvalue') else stream


# ----------------
# UTILITY METHODS
# ----------------

def start_thread(method, *args, **kwargs):
    thread = FuncThread(method, *args, **kwargs)
    thread.start()
    TMP_THREADS.append(thread)
    return thread


def synchronized(lock=None):
    """
    Synchronization decorator as described in
    http://blog.dscpl.com.au/2014/01/the-missing-synchronized-decorator.html.
    """
    def _decorator(wrapped):
        @functools.wraps(wrapped)
        def _wrapper(*args, **kwargs):
            with lock:
                return wrapped(*args, **kwargs)
        return _wrapper
    return _decorator


def is_string(s, include_unicode=True, exclude_binary=False):
    if isinstance(s, six.binary_type) and exclude_binary:
        return False
    if isinstance(s, str):
        return True
    if include_unicode and isinstance(s, six.text_type):
        return True
    return False


def is_string_or_bytes(s):
    return is_string(s) or isinstance(s, six.string_types) or isinstance(s, bytes)


def is_base64(s):
    regex = r'^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$'
    return is_string(s) and re.match(regex, s)


def md5(string):
    m = hashlib.md5()
    m.update(to_bytes(string))
    return m.hexdigest()


def select_attributes(object, attributes):
    return dict([(k, v) for k, v in object.items() if k in attributes])


def in_docker():
    return config.in_docker()


def has_docker():
    try:
        run('docker ps')
        return True
    except Exception:
        return False


def get_docker_container_names():
    return bootstrap.get_docker_container_names()


def path_from_url(url):
    return '/%s' % str(url).partition('://')[2].partition('/')[2] if '://' in url else url


def is_port_open(port_or_url, http_path=None, expect_success=True, protocols=['tcp']):
    port = port_or_url
    host = 'localhost'
    protocol = 'http'
    protocols = protocols if isinstance(protocols, list) else [protocols]
    if isinstance(port, six.string_types):
        url = urlparse(port_or_url)
        port = url.port
        host = url.hostname
        protocol = url.scheme
    nw_protocols = []
    nw_protocols += ([socket.SOCK_STREAM] if 'tcp' in protocols else [])
    nw_protocols += ([socket.SOCK_DGRAM] if 'udp' in protocols else [])
    for nw_protocol in nw_protocols:
        with closing(socket.socket(socket.AF_INET, nw_protocol)) as sock:
            sock.settimeout(1)
            if nw_protocol == socket.SOCK_DGRAM:
                try:
                    if port == 53:
                        dnshost = '127.0.0.1' if host == 'localhost' else host
                        resolver = dns.resolver.Resolver()
                        resolver.nameservers = [dnshost]
                        resolver.timeout = 1
                        resolver.lifetime = 1
                        answers = resolver.query('google.com', 'A')
                        assert len(answers) > 0
                    else:
                        sock.sendto(bytes(), (host, port))
                        sock.recvfrom(1024)
                except Exception:
                    return False
            elif nw_protocol == socket.SOCK_STREAM:
                result = sock.connect_ex((host, port))
                if result != 0:
                    return False
    if 'tcp' not in protocols or not http_path:
        return True
    url = '%s://%s:%s%s' % (protocol, host, port, http_path)
    try:
        response = safe_requests.get(url, verify=False)
        return not expect_success or response.status_code < 400
    except Exception:
        return False


def wait_for_port_open(port, http_path=None, expect_success=True, retries=10, sleep_time=0.5):
    """ Ping the given network port until it becomes available (for a given number of retries).
        If 'http_path' is set, make a GET request to this path and assert a non-error response. """
    def check():
        if not is_port_open(port, http_path=http_path, expect_success=expect_success):
            raise Exception()

    return retry(check, sleep=sleep_time, retries=retries)


def get_free_tcp_port(blacklist=None):
    blacklist = blacklist or []
    for i in range(10):
        tcp = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        tcp.bind(('', 0))
        addr, port = tcp.getsockname()
        tcp.close()
        if port not in blacklist:
            return port
    raise Exception('Unable to determine free TCP port with blacklist %s' % blacklist)


def sleep_forever():
    while True:
        time.sleep(1)


def get_service_protocol():
    return 'https' if config.USE_SSL else 'http'


def timestamp(time=None, format=TIMESTAMP_FORMAT):
    if not time:
        time = datetime.utcnow()
    if isinstance(time, six.integer_types + (float, )):
        time = datetime.fromtimestamp(time)
    return time.strftime(format)


def timestamp_millis(time=None):
    microsecond_time = timestamp(time=time, format=TIMESTAMP_FORMAT_MICROS)
    # truncating microseconds to milliseconds, while leaving the "Z" indicator
    return microsecond_time[:-4] + microsecond_time[-1]


def retry(function, retries=3, sleep=1, sleep_before=0, **kwargs):
    raise_error = None
    if sleep_before > 0:
        time.sleep(sleep_before)
    for i in range(0, retries + 1):
        try:
            return function(**kwargs)
        except Exception as error:
            raise_error = error
            time.sleep(sleep)
    raise raise_error


def dump_thread_info():
    for t in threading.enumerate():
        print(t)
    print(run("ps aux | grep 'node\\|java\\|python'"))


def merge_recursive(source, destination, none_values=[None]):
    for key, value in source.items():
        if isinstance(value, dict):
            # get node or create one
            node = destination.setdefault(key, {})
            merge_recursive(value, node)
        else:
            if not isinstance(destination, dict):
                LOG.warning('Destination for merging %s=%s is not dict: %s' %
                    (key, value, destination))
            if destination.get(key) in none_values:
                destination[key] = value
    return destination


def merge_dicts(*dicts, **kwargs):
    """ Merge all dicts in `*dicts` into a single dict, and return the result. If any of the entries
        in `*dicts` is None, and `default` is specified as keyword argument, then return `default`. """
    result = {}
    for d in dicts:
        if d is None and 'default' in kwargs:
            return kwargs['default']
        if d:
            result.update(d)
    return result


def recurse_object(obj, func, path=''):
    """ Recursively apply `func` to `obj` (may be a list, dict, or other object). """
    obj = func(obj, path=path)
    if isinstance(obj, list):
        for i in range(len(obj)):
            tmp_path = '%s[%s]' % (path or '.', i)
            obj[i] = recurse_object(obj[i], func, tmp_path)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            tmp_path = '%s%s' % ((path + '.') if path else '', k)
            obj[k] = recurse_object(v, func, tmp_path)
    return obj


def base64_to_hex(b64_string):
    return binascii.hexlify(base64.b64decode(b64_string))


def obj_to_xml(obj):
    """ Return an XML representation of the given object (dict, list, or primitive).
        Does NOT add a common root element if the given obj is a list.
        Does NOT work for nested dict structures. """
    if isinstance(obj, list):
        return ''.join([obj_to_xml(o) for o in obj])
    if isinstance(obj, dict):
        return ''.join(['<{k}>{v}</{k}>'.format(k=k, v=obj_to_xml(v)) for (k, v) in obj.items()])
    return str(obj)


def now_utc(millis=False):
    return mktime(datetime.utcnow(), millis=millis)


def now(millis=False):
    return mktime(datetime.now(), millis=millis)


def mktime(timestamp, millis=False):
    if millis:
        epoch = datetime.utcfromtimestamp(0)
        return (timestamp - epoch).total_seconds()
    return calendar.timegm(timestamp.timetuple())


def mkdir(folder):
    if not os.path.exists(folder):
        try:
            os.makedirs(folder)
        except OSError as err:
            # Ignore rare 'File exists' race conditions.
            if err.errno != 17:
                raise


def ensure_readable(file_path, default_perms=None):
    if default_perms is None:
        default_perms = 0o644
    try:
        with open(file_path, 'rb'):
            pass
    except Exception:
        LOG.info('Updating permissions as file is currently not readable: %s' % file_path)
        os.chmod(file_path, default_perms)


def chown_r(path, user):
    """ Recursive chown """
    uid = pwd.getpwnam(user).pw_uid
    gid = grp.getgrnam(user).gr_gid
    os.chown(path, uid, gid)
    for root, dirs, files in os.walk(path):
        for dirname in dirs:
            os.chown(os.path.join(root, dirname), uid, gid)
        for filename in files:
            os.chown(os.path.join(root, filename), uid, gid)


def chmod_r(path, mode):
    """ Recursive chmod """
    os.chmod(path, mode)
    for root, dirnames, filenames in os.walk(path):
        for dirname in dirnames:
            os.chmod(os.path.join(root, dirname), mode)
        for filename in filenames:
            os.chmod(os.path.join(root, filename), mode)


def rm_rf(path):
    """
    Recursively removes a file or directory
    """
    if not path or not os.path.exists(path):
        return
    # Running the native command can be an order of magnitude faster in Alpine on Travis-CI
    if is_alpine():
        try:
            return run('rm -rf "%s"' % path)
        except Exception:
            pass
    # Make sure all files are writeable and dirs executable to remove
    chmod_r(path, 0o777)
    # check if the file is either a normal file, or, e.g., a fifo
    exists_but_non_dir = os.path.exists(path) and not os.path.isdir(path)
    if os.path.isfile(path) or exists_but_non_dir:
        os.remove(path)
    else:
        shutil.rmtree(path)


def cp_r(src, dst):
    """Recursively copies file/directory"""
    if os.path.isfile(src):
        shutil.copy(src, dst)
    else:
        shutil.copytree(src, dst)


def download(url, path, verify_ssl=True):
    """Downloads file at url to the given path"""
    # make sure we're creating a new session here to
    # enable parallel file downloads during installation!
    s = requests.Session()
    r = s.get(url, stream=True, verify=verify_ssl)
    # check status code before attempting to read body
    if r.status_code >= 400:
        raise Exception('Failed to download %s, response code %s' % (url, r.status_code))

    total = 0
    try:
        if not os.path.exists(os.path.dirname(path)):
            os.makedirs(os.path.dirname(path))
        LOG.debug('Starting download from %s to %s (%s bytes)' % (url, path, r.headers.get('content-length')))
        with open(path, 'wb') as f:
            for chunk in r.iter_content(DOWNLOAD_CHUNK_SIZE):
                total += len(chunk)
                if chunk:  # filter out keep-alive new chunks
                    f.write(chunk)
                    LOG.debug('Writing %s bytes (total %s) to %s' % (len(chunk), total, path))
                else:
                    LOG.debug('Empty chunk %s (total %s) from %s' % (chunk, total, url))
            f.flush()
            os.fsync(f)
        if os.path.getsize(path) == 0:
            LOG.warning('Zero bytes downloaded from %s, retrying' % url)
            download(url, path, verify_ssl)
            return
        LOG.debug('Done downloading %s, response code %s, total bytes %d' % (url, r.status_code, total))
    finally:
        r.close()
        s.close()


def first_char_to_lower(s):
    return '%s%s' % (s[0].lower(), s[1:])


def is_number(s):
    try:
        float(s)  # for int, long and float
        return True
    except (TypeError, ValueError):
        return False


def is_mac_os():
    return bootstrap.is_mac_os()


def is_linux():
    return bootstrap.is_linux()


def is_alpine():
    try:
        if '_is_alpine_' not in CACHE:
            CACHE['_is_alpine_'] = False
            if not os.path.exists('/etc/issue'):
                return False
            out = to_str(subprocess.check_output('cat /etc/issue', shell=True))
            CACHE['_is_alpine_'] = 'Alpine' in out
    except subprocess.CalledProcessError:
        return False
    return CACHE['_is_alpine_']


def get_arch():
    if is_mac_os():
        return 'osx'
    if is_alpine():
        return 'alpine'
    if is_linux():
        return 'linux'
    raise Exception('Unable to determine system architecture')


def short_uid():
    return str(uuid.uuid4())[0:8]


def long_uid():
    return str(uuid.uuid4())


def json_safe(item):
    """ return a copy of the given object (e.g., dict) that is safe for JSON dumping """
    try:
        return json.loads(json.dumps(item, cls=CustomEncoder))
    except Exception:
        item = fix_json_keys(item)
        return json.loads(json.dumps(item, cls=CustomEncoder))


def fix_json_keys(item):
    """ make sure the keys of a JSON are strings (not binary type or other) """
    item_copy = item
    if isinstance(item, list):
        item_copy = []
        for i in item:
            item_copy.append(fix_json_keys(i))
    if isinstance(item, dict):
        item_copy = {}
        for k, v in item.items():
            item_copy[to_str(k)] = fix_json_keys(v)
    return item_copy


def canonical_json(obj):
    return json.dumps(obj, sort_keys=True)


def save_file(file, content, append=False):
    mode = 'a' if append else 'w+'
    if not isinstance(content, six.string_types):
        mode = mode + 'b'
    with open(file, mode) as f:
        f.write(content)
        f.flush()


def load_file(file_path, default=None, mode=None):
    if not os.path.isfile(file_path):
        return default
    if not mode:
        mode = 'r'
    with open(file_path, mode) as f:
        result = f.read()
    return result


def to_str(obj, encoding=DEFAULT_ENCODING, errors='strict'):
    """ If ``obj`` is an instance of ``binary_type``, return
    ``obj.decode(encoding, errors)``, otherwise return ``obj`` """
    return obj.decode(encoding, errors) if isinstance(obj, six.binary_type) else obj


def to_bytes(obj, encoding=DEFAULT_ENCODING, errors='strict'):
    """ If ``obj`` is an instance of ``text_type``, return
    ``obj.encode(encoding, errors)``, otherwise return ``obj`` """
    return obj.encode(encoding, errors) if isinstance(obj, six.text_type) else obj


def cleanup(files=True, env=ENV_DEV, quiet=True):
    if files:
        cleanup_tmp_files()


def cleanup_threads_and_processes(quiet=True, debug=False):
    for thread in TMP_THREADS:
        if thread:
            try:
                print_debug('[shutdown] Cleaning up thread: %s' % thread, debug)
                if hasattr(thread, 'shutdown'):
                    thread.shutdown()
                    continue
                if hasattr(thread, 'kill'):
                    thread.kill()
                    continue
                thread.stop(quiet=quiet)
            except Exception as e:
                print(e)
    for proc in TMP_PROCESSES:
        try:
            print_debug('[shutdown] Cleaning up process: %s' % proc, debug)
            proc.terminate()
        except Exception as e:
            print(e)
    # clean up async tasks
    try:
        import asyncio
        for task in asyncio.all_tasks():
            try:
                print_debug('[shutdown] Canceling asyncio task: %s' % task, debug)
                task.cancel()
            except Exception as e:
                print(e)
    except Exception:
        pass
    print_debug('[shutdown] Done cleaning up threads / processes / tasks', debug)
    # clear lists
    clear_list(TMP_THREADS)
    clear_list(TMP_PROCESSES)


def clear_list(list_obj):
    while len(list_obj):
        del list_obj[0]


def cleanup_tmp_files():
    for tmp in TMP_FILES:
        try:
            rm_rf(tmp)
        except Exception:
            pass  # file likely doesn't exist, or permission denied
    del TMP_FILES[:]


def new_tmp_file():
    """ Return a path to a new temporary file. """
    tmp_file, tmp_path = tempfile.mkstemp()
    os.close(tmp_file)
    TMP_FILES.append(tmp_path)
    return tmp_path


def new_tmp_dir():
    folder = new_tmp_file()
    rm_rf(folder)
    mkdir(folder)
    return folder


def is_ip_address(addr):
    try:
        socket.inet_aton(addr)
        return True
    except socket.error:
        return False


def is_zip_file(content):
    stream = BytesIO(content)
    return zipfile.is_zipfile(stream)


def unzip(path, target_dir, overwrite=True):
    if is_alpine():
        # Running the native command can be an order of magnitude faster in Alpine on Travis-CI
        flags = '-o' if overwrite else ''
        return run('cd %s; unzip %s %s' % (target_dir, flags, path))
    try:
        zip_ref = zipfile.ZipFile(path, 'r')
    except Exception as e:
        LOG.warning('Unable to open zip file: %s: %s' % (path, e))
        raise e
    # Make sure to preserve file permissions in the zip file
    # https://www.burgundywall.com/post/preserving-file-perms-with-python-zipfile-module
    try:
        for file_entry in zip_ref.infolist():
            _unzip_file_entry(zip_ref, file_entry, target_dir)
    finally:
        zip_ref.close()


def _unzip_file_entry(zip_ref, file_entry, target_dir):
    """
    Extracts a Zipfile entry and preserves permissions
    """
    zip_ref.extract(file_entry.filename, path=target_dir)
    out_path = os.path.join(target_dir, file_entry.filename)
    perm = file_entry.external_attr >> 16
    os.chmod(out_path, perm or 0o777)


def untar(path, target_dir):
    mode = 'r:gz' if path.endswith('gz') else 'r'
    with tarfile.open(path, mode) as tar:
        tar.extractall(path=target_dir)


def zip_contains_jar_entries(content, jar_path_prefix=None, match_single_jar=True):
    try:
        with tempfile.NamedTemporaryFile() as tf:
            tf.write(content)
            tf.flush()
            with zipfile.ZipFile(tf.name, 'r') as zf:
                jar_entries = [e for e in zf.infolist() if e.filename.lower().endswith('.jar')]
                if match_single_jar and len(jar_entries) == 1 and len(zf.infolist()) == 1:
                    return True
                matching_prefix = [e for e in jar_entries if
                    not jar_path_prefix or e.filename.lower().startswith(jar_path_prefix)]
                return len(matching_prefix) > 0
    except Exception:
        return False


def is_jar_archive(content):
    """ Determine whether `content` contains valid zip bytes representing a JAR archive
        that contains at least one *.class file and a META-INF/MANIFEST.MF file. """
    try:
        with tempfile.NamedTemporaryFile() as tf:
            tf.write(content)
            tf.flush()
            with zipfile.ZipFile(tf.name, 'r') as zf:
                class_files = [e for e in zf.infolist() if e.filename.endswith('.class')]
                manifest_file = [e for e in zf.infolist() if e.filename.upper() == 'META-INF/MANIFEST.MF']
                if not class_files or not manifest_file:
                    return False
    except Exception:
        return False
    return True


def is_root():
    out = run('whoami').strip()
    return out == 'root'


def cleanup_resources(debug=False):
    cleanup_tmp_files()
    cleanup_threads_and_processes(debug=debug)


def print_debug(msg, debug=False):
    if debug:
        print(msg)


@synchronized(lock=SSL_CERT_LOCK)
def generate_ssl_cert(target_file=None, overwrite=False, random=False, return_content=False, serial_number=None):
    # Note: Do NOT import "OpenSSL" at the root scope
    # (Our test Lambdas are importing this file but don't have the module installed)
    from OpenSSL import crypto

    def all_exist(*files):
        return all([os.path.exists(f) for f in files])

    def store_cert_key_files(base_filename):
        key_file_name = '%s.key' % base_filename
        cert_file_name = '%s.crt' % base_filename
        # extract key and cert from target_file and store into separate files
        content = load_file(target_file)
        key_start = '-----BEGIN PRIVATE KEY-----'
        key_end = '-----END PRIVATE KEY-----'
        cert_start = '-----BEGIN CERTIFICATE-----'
        cert_end = '-----END CERTIFICATE-----'
        key_content = content[content.index(key_start): content.index(key_end) + len(key_end)]
        cert_content = content[content.index(cert_start): content.rindex(cert_end) + len(cert_end)]
        save_file(key_file_name, key_content)
        save_file(cert_file_name, cert_content)
        return cert_file_name, key_file_name

    if target_file and not overwrite and os.path.exists(target_file):
        key_file_name = ''
        cert_file_name = ''
        try:
            cert_file_name, key_file_name = store_cert_key_files(target_file)
        except Exception as e:
            # fall back to temporary files if we cannot store/overwrite the files above
            LOG.info('Error storing key/cert SSL files (falling back to random tmp file names): %s' % e)
            target_file_tmp = new_tmp_file()
            cert_file_name, key_file_name = store_cert_key_files(target_file_tmp)
        if all_exist(cert_file_name, key_file_name):
            return target_file, cert_file_name, key_file_name
    if random and target_file:
        if '.' in target_file:
            target_file = target_file.replace('.', '.%s.' % short_uid(), 1)
        else:
            target_file = '%s.%s' % (target_file, short_uid())

    # create a key pair
    k = crypto.PKey()
    k.generate_key(crypto.TYPE_RSA, 2048)

    # create a self-signed cert
    cert = crypto.X509()
    subj = cert.get_subject()
    subj.C = 'AU'
    subj.ST = 'Some-State'
    subj.L = 'Some-Locality'
    subj.O = 'LocalStack Org'  # noqa
    subj.OU = 'Testing'
    subj.CN = 'localhost'
    # Note: new requirements for recent OSX versions: https://support.apple.com/en-us/HT210176
    # More details: https://www.iol.unh.edu/blog/2019/10/10/macos-catalina-and-chrome-trust
    serial_number = serial_number or 1001
    cert.set_version(2)
    cert.set_serial_number(serial_number)
    cert.gmtime_adj_notBefore(0)
    cert.gmtime_adj_notAfter(2 * 365 * 24 * 60 * 60)
    cert.set_issuer(cert.get_subject())
    cert.set_pubkey(k)
    alt_names = b'DNS:localhost,DNS:test.localhost.atlassian.io,IP:127.0.0.1'
    cert.add_extensions([
        crypto.X509Extension(b'subjectAltName', False, alt_names),
        crypto.X509Extension(b'basicConstraints', True, b'CA:false'),
        crypto.X509Extension(b'keyUsage', True, b'nonRepudiation,digitalSignature,keyEncipherment'),
        crypto.X509Extension(b'extendedKeyUsage', True, b'serverAuth')
    ])
    cert.sign(k, 'SHA256')

    cert_file = StringIO()
    key_file = StringIO()
    cert_file.write(to_str(crypto.dump_certificate(crypto.FILETYPE_PEM, cert)))
    key_file.write(to_str(crypto.dump_privatekey(crypto.FILETYPE_PEM, k)))
    cert_file_content = cert_file.getvalue().strip()
    key_file_content = key_file.getvalue().strip()
    file_content = '%s\n%s' % (key_file_content, cert_file_content)
    if target_file:
        key_file_name = '%s.key' % target_file
        cert_file_name = '%s.crt' % target_file
        # check existence to avoid permission denied issues:
        # https://github.com/localstack/localstack/issues/1607
        if not all_exist(target_file, key_file_name, cert_file_name):
            for i in range(2):
                try:
                    save_file(target_file, file_content)
                    save_file(key_file_name, key_file_content)
                    save_file(cert_file_name, cert_file_content)
                    break
                except Exception as e:
                    if i > 0:
                        raise
                    LOG.info('Unable to store certificate file under %s, using tmp file instead: %s' % (target_file, e))
                    # Fix for https://github.com/localstack/localstack/issues/1743
                    target_file = '%s.pem' % new_tmp_file()
                    key_file_name = '%s.key' % target_file
                    cert_file_name = '%s.crt' % target_file
            TMP_FILES.append(target_file)
            TMP_FILES.append(key_file_name)
            TMP_FILES.append(cert_file_name)
        if not return_content:
            return target_file, cert_file_name, key_file_name
    return file_content


def run_safe(_python_lambda, print_error=False, **kwargs):
    try:
        return _python_lambda(**kwargs)
    except Exception as e:
        if print_error:
            LOG.warning('Unable to execute function: %s' % e)


def run_cmd_safe(**kwargs):
    return run_safe(run, print_error=False, **kwargs)


def run(cmd, cache_duration_secs=0, **kwargs):

    def do_run(cmd):
        return bootstrap.run(cmd, **kwargs)

    if cache_duration_secs <= 0:
        return do_run(cmd)

    hash = md5(cmd)
    cache_file = CACHE_FILE_PATTERN.replace('*', hash)
    mkdir(os.path.dirname(CACHE_FILE_PATTERN))
    if os.path.isfile(cache_file):
        # check file age
        mod_time = os.path.getmtime(cache_file)
        time_now = now()
        if mod_time > (time_now - cache_duration_secs):
            f = open(cache_file)
            result = f.read()
            f.close()
            return result
    result = do_run(cmd)
    f = open(cache_file, 'w+')
    f.write(result)
    f.close()
    clean_cache()
    return result


def clone(item):
    return json.loads(json.dumps(item))


def clone_safe(item):
    return clone(json_safe(item))


def remove_non_ascii(text):
    # text = unicode(text, "utf-8")
    text = text.decode('utf-8', CODEC_HANDLER_UNDERSCORE)
    # text = unicodedata.normalize('NFKD', text)
    text = text.encode('ascii', CODEC_HANDLER_UNDERSCORE)
    return text


class NetrcBypassAuth(requests.auth.AuthBase):
    def __call__(self, r):
        return r


class _RequestsSafe(type):
    """ Wrapper around requests library, which can prevent it from verifying
    SSL certificates or reading credentials from ~/.netrc file """
    verify_ssl = True

    def __getattr__(self, name):
        method = requests.__dict__.get(name.lower())
        if not method:
            return method

        def _wrapper(*args, **kwargs):
            if 'auth' not in kwargs:
                kwargs['auth'] = NetrcBypassAuth()
            if not self.verify_ssl and args[0].startswith('https://') and 'verify' not in kwargs:
                kwargs['verify'] = False
            return method(*args, **kwargs)
        return _wrapper


# create class-of-a-class
class safe_requests(with_metaclass(_RequestsSafe)):
    pass


def make_http_request(url, data=None, headers=None, method='GET'):

    if is_string(method):
        method = requests.__dict__[method.lower()]

    return method(url, headers=headers, data=data, auth=NetrcBypassAuth(), verify=False)


class SafeStringIO(io.StringIO):
    """ Safe StringIO implementation that doesn't fail if str is passed in Python 2. """
    def write(self, obj):
        if six.PY2 and isinstance(obj, str):
            obj = obj.decode('unicode-escape')
        return super(SafeStringIO, self).write(obj)


def clean_cache(file_pattern=CACHE_FILE_PATTERN,
        last_clean_time=last_cache_clean_time, max_age=CACHE_MAX_AGE):

    mutex_clean.acquire()
    time_now = now()
    try:
        if last_clean_time['time'] > time_now - CACHE_CLEAN_TIMEOUT:
            return
        for cache_file in set(glob.glob(file_pattern)):
            mod_time = os.path.getmtime(cache_file)
            if time_now > mod_time + max_age:
                rm_rf(cache_file)
        last_clean_time['time'] = time_now
    finally:
        mutex_clean.release()
    return time_now


def truncate(data, max_length=100):
    return ('%s...' % data[:max_length]) if len(data or '') > max_length else data


def escape_html(string, quote=False):
    if six.PY2:
        import cgi
        return cgi.escape(string, quote=quote)
    import html
    return html.escape(string, quote=quote)


def parallelize(func, list, size=None):
    if not size:
        size = len(list)
    if size <= 0:
        return None
    pool = Pool(size)
    result = pool.map(func, list)
    pool.close()
    pool.join()
    return result


def isoformat_milliseconds(t):
    try:
        return t.isoformat(timespec='milliseconds')
    except TypeError:
        return t.isoformat()[:-3]


# Code that requires util functions from above
CACHE_FILE_PATTERN = CACHE_FILE_PATTERN.replace('_random_dir_', short_uid())
