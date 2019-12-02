import subprocess
from shutil import copyfile
import importlib.util
import sys
import os
import io
import glob
import shlex
import re
import json
import yaml
import tempfile
import random
import string
import logging
from . import filters
from . import pipelines

logger = logging.getLogger("evaluator")

def env_build(env):
    if not env:
        env = {}

    return " ".join([shlex.quote(f"-E{k}={v}") for k, v in env.items()])

def rand_str(N):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=N))


def compare(actual, expected, used_filters):
    return filters.apply_filters(actual, used_filters) == filters.apply_filters(expected, used_filters)


def load_module(path):
    module_name = "xyz"

    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module

class TempFile:
    def __init__(self, suffix, dir):
        self.suffix = suffix
        self.dir = dir
        self.path = None
        self.fd = None

    def __enter__(self):
        self.path = os.path.join(self.dir, f'{rand_str(5)}_{self.suffix}')
        self.fd = open(self.path, 'w+')
        return self.fd

    def __exit__(self, type, value, traceback):
        self.fd.close()
        os.remove(self.path)

class File:
    def __init__(self, path):
        self.path = path

    def open(self, mode='r'):
        if isinstance(self.path, io.StringIO):
            return io.StringIO(self.path.getvalue())
        return open(self.path)

    def read(self):
        with self.open() as f:
            return f.read()


class Test:
    def __init__(self, name):
        self.name = name
        self.stdin = None
        self.stdout = None
        self.stderr = None
        self.args = []
        self.exit_code = 0
        self.files = []
        self.check = None
        self.filters = []
        self.limits = {}
        self._title = None
        self.script = None
        self.stdio_max_bytes = 100 * 1024

    @property
    def escaped_args(self):
        return " ".join(map(shlex.quote, self.args))

    @property
    def title(self):
        return self._title if self._title else self.name
    
    @title.setter
    def title(self, value):
        self._title = value


class Evaluation:
    def __init__(self, task_path : str, result_path: str, sandbox, meta=None):
        self.task_path = task_path
        self.sandbox = sandbox
        self.filters = []
        self.limits = {
            'wall-time': 0.5,
            'time': 0,
            'processes': 10,
            'stack': 0,
            'cg-mem': 5 * 1024 * 1024,
            'fsize': 1024 * 1024,
        }
        self.meta = meta if meta else {}
        self.tests_dict = {}
        self.File = File
        self.load_tests()

        #os.makedirs(result_path)

    @property
    def tests(self):
        return self.tests_dict.values()

    def create_test(self, name):
        if name in self.tests_dict:
            return self.tests_dict[name]

        t = Test(name)

        path = os.path.join(self.task_path, f"{name}.out")
        if os.path.exists(path):
            t.stdout = File(path)

        path = os.path.join(self.task_path, f"{name}.err")
        if os.path.exists(path):
            t.stderr = File(path)

        stdin_path = os.path.join(self.task_path, f"{name}.in")
        if os.path.exists(stdin_path):
            t.stdin = File(stdin_path)

        path = os.path.join(self.task_path, f"{name}.test.py")
        if os.path.exists(path):
            t.script = load_module(path)

        self.tests_dict[name] = t
        return t

    def load_tests(self):
        for ext in ['out', 'err', 'test.py']:
            for out in glob.glob(os.path.join(self.task_path, f"*.{ext}")):
                test_name = os.path.basename(re.sub(f".{ext}$", '', out))
                self.create_test(test_name)

        try:
            with open(os.path.join(self.task_path, 'config.yml')) as f:
                conf = yaml.load(f.read(), Loader=yaml.SafeLoader)
                if conf:
                    for filter_name in conf.get('filters', []):
                        self.filters.append(filters.all_filters[filter_name.lower()]())

                    for k, v in conf.get('limits', {}).items():
                        if k not in self.limits:
                            logging.error(f'unknown limit {k}')
                        else:
                            self.limits[k] = v


                    for test_conf in conf.get('tests', []):
                        t = self.create_test(str(test_conf.get('name', f'test {len(self.tests)}')))
                        t.title = test_conf.get('title', t.name)
                        t.exit_code = test_conf.get('exit_code', 0)
                        t.args = [str(s) for s in test_conf.get('args', [])]
                        files = test_conf.get('files', [])
                        for f in files:
                            t.files.append({
                                'path': f['path'],
                                'expected': File(os.path.join(self.task_path, f['expected'])),
                            })

        except FileNotFoundError:
            pass

        path = os.path.join(self.task_path, 'script.py')
        if os.path.exists(path):
            script = load_module(path)
            generate_tests = getattr(script, 'gen_tests', None)
            if generate_tests:
                generate_tests(self)

    def task_file(self, path):
        return os.path.join(self.task_path, path)

    def evaluate(self, test: Test, env=None, title=None):
        result = {
             'name': test.name,
             'title': title if title else test.title,
             'success': True,
             'fail_reason': [],
        }

        args = {}
        if test.stdin:
            args['stdin'] = test.stdin.open()
            result['stdin'] = args['stdin'].read()
            args['stdin'].seek(0)

        cmd = ['./main'] + test.args
        flags = " ".join([shlex.quote(f"--{k}={v}") for k, v in self.limits.items()])
        isolate_cmd = shlex.split(f"isolate -M /tmp/meta --cg {flags} -s --run {env_build(env)} --") + cmd
        logging.debug("executing in isolation: {}", isolate_cmd)
        p = subprocess.Popen(isolate_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **args)
        result['stdout'], result['stderr'] = p.communicate()

        if test.stdin:
            args['stdin'].close()

        result['exit_code'] = p.returncode
        result['stdout'] = result['stdout'][0:test.stdio_max_bytes].decode('utf-8')
        result['stderr'] = result['stderr'][0:test.stdio_max_bytes].decode('utf-8')

        p.stdout.close()
        p.stderr.close()

        filters = self.filters + test.filters

        if test.stdout:
            with test.stdout.open() as f:
                result['stdout_expected'] = f.read()
            success = compare(result['stdout'], result['stdout_expected'], filters)
            result['success'] &= success
            if not success:
                result['fail_reason'].append('stdout not matches')

        if test.stderr:
            with test.stderr.open() as f:
                result['stderr_expected'] = f.read()
            success = compare(result['stderr'], result['stderr_expected'], filters)
            result['success'] &= success
            if not success:
                result['fail_reason'].append('stderr not matches')

        result['files'] = []
        for f in test.files:
            try:
                with self.sandbox.open(f['path']) as cur:
                    content = cur.read()
                    expected = f['expected'].read()
                    same = compare(content, expected, filters)

                    result['files'].append({
                        'path': f['path'],
                        'content': content,
                        'expected': expected,
                        'success': same,
                    })

                    result['success'] &= same
            except FileNotFoundError as e:
                result['files'].append({
                    'path': f['path'],
                    'expected': f['expected'].read(),
                    'success': False,
                    'error': 'file not found',
                })

                result['success'] &= False



        with open('/tmp/meta') as f:
            for line in f:
                key, val = line.split(':', 1)
                key = key.strip().replace('-', '')
                val = val.strip()

                if key == 'exitcode':
                    result['exit_code'] = int(val)
                else:
                    result[key] = val

        result['command'] = ' '.join(cmd)
        if test.stdin:
            result['command'] += f' < {shlex.quote(os.path.basename(test.stdin.path))}'

        if test.script:
            check = getattr(test.script, 'check', None)
            if check:
                result['success'] &= check(result, self)

        return result

class Sandbox:
    def __init__(self):
        subprocess.check_call(["isolate", "--cleanup"])
        self.path = subprocess.check_output(["isolate", "--init", "--cg"]).decode('utf-8').strip()

    def system_path(self, path=''):
        return os.path.join(os.path.join(self.path, 'box'), path)

    def run(self, cmd, env=None):
        isolation_cmd = f"isolate -s --run --processes=100 {env_build(env)} -e -- {cmd}"
        logger.info(f"executing in isolation: {isolation_cmd}")

        p = subprocess.Popen(shlex.split(isolation_cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate()

        res = {
            'exit_code': p.returncode,
            'stdout': stdout.decode('utf-8'),
            'stderr': stderr.decode('utf-8'),
        }
        logger.info(f"exit_code: {p.returncode}")

        p.stdout.close()
        p.stderr.close()

        return res

    def open(self, path, mode='r'):
        return open(self.system_path(path), mode)

    def open_temporary(self, suffix):
        return TempFile(suffix, self.system_path())

    def copy(self, local, box):
        copyfile(local, self.system_path(box))

    def run_check(self, cmd):
        ret = self.run(cmd)
        if ret['exit_code'] != 0:
            raise "failed to execute:" + cmd
        return ret

    def compile(self, flags = None, sources=None):
        if not sources:
            sources = [os.path.relpath(p, self.path + '/box') for p in glob.glob(self.system_path('*.c'))]

        if not flags:
            flags = []
        flags = ['-g', '-lm', '-Wall', '-pedantic'] + flags
        
        command = '/usr/bin/gcc {sources} -o main {flags}'.format(
            sources=' '.join(map(shlex.quote, sources)),
            flags=' '.join(map(shlex.quote, flags))
        ).strip()

        result = self.run(command)
        result['command'] = command
        return result

def evaluate(task_path, submit_path, result_path, meta=None):
    '''
    Called by Django.
    '''

    sandbox = Sandbox()
    evaluation = Evaluation(task_path, result_path, sandbox, meta)

    logger.info(f"evaluating {submit_path}")
    copyfile(submit_path, os.path.join(sandbox.path, "box/submit"))

    pipeline = [
        ('download', pipelines.DownloadPipe()),
        ('normal run', pipelines.GccPipeline()),
        ('run with sanitizer', pipelines.GccPipeline(['-fsanitize=address', '-fsanitize=bounds', '-fsanitize=undefined'])),
        #('malloc fail tester', Mallocer()),
        #('random inputs', InputGeneratorPipe())
    ]
    
    result = []
    for name, pipe in pipeline:
        logger.info(f"executing {name}")
        res = pipe.run(evaluation)
        if res:
            result.append({'name': name, **res})

    return result

if __name__ == "__main__":
    import argparse
    from pprint import pprint
    import shutil

    logger.setLevel(logging.DEBUG)
    parser = argparse.ArgumentParser()
    parser.add_argument('task_dir', help='path to directory with the task')
    parser.add_argument('solution', help='path to source code in .c or tar')
    parser.add_argument('--print-json')

    result_dir = '/tmp/eval'
    try:
        shutil.rmtree(result_dir)
    except FileNotFoundError:
        pass

    args = parser.parse_args()
    result = evaluate(args.task_dir, args.solution, result_dir)

    if args.print_json:
        pprint(result)