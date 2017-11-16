#!/usr/bin/python3

import argparse
import copy
import logging
import multiprocessing
import logging.config
import os
import random
import raven
import signal
import subprocess
import sys
import time
import yaml
import github3
import traceback
import redis

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
from prci_github import TaskQueue, AbstractJob, JobResult
from prci_github.adapter import GitHubAdapter


SENTRY_URL = 'https://d24d8d622cbb4e2ea447c9a64f19b81a:4db0ce47706f435bb3f8a02a0a1f2e22@sentry.io/193222'
ERROR_BACKOFF_TIME = 600
REBOOT_DELAY = 3600 * 3
REBOOT_TIME_FILE = '/root/next_reboot'
REBOOT_CHECK_INTERVAL = 300
TASK_MIN_CPU = 2
TASK_MIN_MEM = 900  # in MiB
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class Job(AbstractJob):
    def __call__(self, depends_results={}):
        url = None
        description = None

        dep_results = {}
        for task_name, result in depends_results.items():
            desc_key = '{}_description'.format(task_name)
            url_key = '{}_url'.format(task_name)
            dep_results[desc_key] = result.description
            dep_results[url_key] = result.url

        cmd = self.job.format(target_refspec=self.target, **dep_results)

        try:
            url = subprocess.check_output(cmd, shell=True)
        except subprocess.CalledProcessError as err:
            logging.error(traceback.print_exc())
            if err.returncode == 1:
                state = 'error'
                url = ''
            else:
                raise
        else:
            state = 'success'
            description = ''

        return JobResult(state, description, url)


class JobDispatcher(AbstractJob):
    def __init__(self, job, build_target, task_name):
        super(JobDispatcher, self).__init__(job, build_target)
        self.klass = getattr(
            __import__('tasks', fromlist=[self.job['class']]),
            self.job['class'])
        self.kwargs = self.job['args']
        self.kwarg_lookup = {
            'git_repo': build_target[0],
            'git_refspec': build_target[1]}
        self.task_name = task_name

    @property
    def timeout(self):
        return self.kwargs.get('timeout') or 0

    def __call__(self, depends_results=None):
        if depends_results is not None:
            for task_name, result in depends_results.items():
                self.kwarg_lookup['{}_description'.format(task_name)] = \
                    result.description
                self.kwarg_lookup['{}_url'.format(task_name)] = result.url

        kwargs = {}
        for key, value in self.kwargs.items():
            if isinstance(value, str):
                value = value.format(**self.kwarg_lookup)
            kwargs[key] = value

        kwargs['task_name'] = self.task_name
        job = self.klass(**kwargs)
        try:
            job()
        except Exception as exc:
            logging.error(traceback.print_exc())
            description = '{type_}: {msg}'.format(type_=type(exc).__name__,
                                                  msg=str(exc))
            state = 'error'

            sentry_report_exception({'module': 'tasks'})
        else:
            description = job.description
            if job.returncode == 0:
                state = 'success'
            else:
                state = 'failure'

        return JobResult(state, description, job.remote_url)


def create_parser():
    def config_file(path):
        def load_yaml(yml_path):
            try:
                with open(yml_path) as yml_file:
                    return yaml.load(yml_file)
            except IOError as exc:
                raise argparse.ArgumentTypeError(
                    'Failed to open {}: {}'.format(yml_path, exc))
            except yaml.YAMLError as exc:
                raise argparse.ArgumentTypeError(
                    'Failed to parse YAML from {}: {}'.format(yml_path, exc))

        config = load_yaml(path)
        try:
            config['credentials']
            config['repository']
            config['tasks_file']
            config['logging']
        except KeyError as exc:
            raise argparse.ArgumentTypeError(
                'Missing required section {} in configuration.', exc)

        try:
            whitelist_file = config.pop('whitelist_file')
        except KeyError:
            logger.warning('No whitelist file supplied. Manual approval will '
                           'be needed for all PRs.')
            config['whitelist'] = []
        else:
            config['whitelist'] = load_yaml(whitelist_file)

        return config

    parser = argparse.ArgumentParser()
    parser.add_argument(
        'ID', type=str,
        help='Unique runner ID',
    )
    parser.add_argument(
        '--config', type=config_file, required=True,
        help='YAML file with complete configuration.',
    )

    return parser


def update_runner(repo, creds):
    cmd = ['git', 'pull']
    stdout = subprocess.check_output(cmd).decode('utf-8')
    if 'Already up-to-date' not in stdout:
        logger.info('Code change detected, re-deploying runner')
        subprocess.call([
            'ansible-playbook',
            '-i', 'ansible/hosts/runner_localhost',
            '-e', 'github_token={}'.format(creds['token']),
            '-e', 'deploy_ssh_key=false',
            'ansible/prepare_test_runners.yml'])


def sentry_report_exception(context=None):
    sentry = raven.Client(SENTRY_URL)

    if context is not None:
        with sentry.context:
            sentry.context.merge(context)
    try:
        sentry.captureException()
    finally:
        sentry.context.clear()


def plan_reboot(delay=REBOOT_DELAY):
    next_reboot = int(time.time()) + delay
    with open(REBOOT_TIME_FILE, 'w') as f:
        f.write(str(next_reboot))


def read_reboot_time():
    try:
        with open(REBOOT_TIME_FILE, 'r') as f:
            return int(f.read())
    except IOError:
        return None


def reboot(repo, creds):
    try:
        update_runner(repo, creds)
    except Exception:
        sentry_report_exception()

    plan_reboot()
    logging.info('Rebooting the machine')
    subprocess.call('reboot', shell=True)


class Scheduler(object):

    def __init__(self, task_queue, reboot_check_interval,
                 no_task_backoff_time):
        self.task_queue = task_queue
        self.reboot_check_interval = reboot_check_interval
        self.no_task_backoff_time = no_task_backoff_time

        self.done = False
        self.should_abort = False
        self.reboot = False
        self.processes = {}

        signal.signal(signal.SIGINT, self.terminate)
        signal.signal(signal.SIGALRM, self.check_reboot)
        signal.alarm(self.reboot_check_interval)

    def terminate(self, *args, **kwargs):
        if self.done:
            return self.abort()
        logging.info('Terminate. Waiting for tasks to finish.')
        self.done = True
        self.reboot = False

    def abort(self):
        if self.should_abort:
            sys.exit('Forced quit. There may be stale files and processes '
                     'left behind.')
        logging.debug('Abort. Waiting for clean up.')
        self.done = True
        self.should_abort = True
        self.reboot = False

    def check_reboot(self, *args, **kwargs):
        if self.done:
            return

        reboot_time = read_reboot_time()
        if reboot_time is None:
            plan_reboot(delay=random.randint(1, REBOOT_DELAY))
            return

        if time.time() > reboot_time:
            self.done = True
            self.reboot = True

    def run(self):
        def join(wait=False):
            procs = copy.copy(self.processes)
            for task, p in procs.items():
                if wait or not p.is_alive():
                    logging.info('Calling join() for process')
                    p.join()
                    self.task_queue.free_resources(task)
                    logging.info('Task {} released resources'.format(task.name))
                    del(self.processes[task])

        def execute_task(task):
            try:
                task.execute()
            except Exception as e:
                logging.error(e)
                sentry_report_exception({'module': 'github'})

        while not self.done:
            join()

            avail_cpu = self.task_queue.available_cpus
            avail_mem = self.task_queue.available_memory

            if avail_cpu < TASK_MIN_CPU or avail_mem < TASK_MIN_MEM:
                logging.debug(
                    "Not enough resources, waiting for tasks to finish (%d"
                    "CPUs, %f MiB RAM)" % (avail_cpu, avail_mem))
                time.sleep(10)
                continue

            tasks = self.task_queue.take_tasks()
            logging.info('Current tasks {}'.format(
                self.task_queue.running_tasks))
            if not tasks:
                time.sleep(self.no_task_backoff_time)
                continue

            for task in tasks:
                p = multiprocessing.Process(target=execute_task, args=(task,))
                p.start()
                self.processes[task] = p

        join(wait=True)


def main():
    parser = create_parser()
    args = parser.parse_args()

    runner_id = args.ID
    config = args.config

    creds = config['credentials']
    repo = config['repository']
    tasks_file = config['tasks_file']
    whitelist = config['whitelist']
    no_task_backoff_time = config['no_task_backoff_time']

    logging.config.dictConfig(config['logging'])

    github = github3.login(token=creds['token'])
    github.session.mount('https://api.github.com',
                         GitHubAdapter(cache=redis.Redis()))

    repo = github.repository(repo['owner'], repo['name'])
    task_queue = TaskQueue(repo, tasks_file, JobDispatcher, runner_id,
                           whitelist)

    task_queue.create_tasks_for_pulls()

    scheduler = Scheduler(task_queue, REBOOT_CHECK_INTERVAL,
                          no_task_backoff_time)

    try:
        scheduler.run()
    except Exception:
        sentry_report_exception({'module': 'github'})
        raise

    if scheduler.reboot:
        try:
            reboot(repo, creds)
        except:
            sentry_report_exception()


if __name__ == '__main__':
    main()
