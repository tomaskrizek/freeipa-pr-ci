#!/usr/bin/python

import abc
import base64
import collections
import datetime
import dateutil.parser
import logging
import parse
import pytz
import requests
import time
import yaml

import cachecontrol
import redis


GITHUB_DESCRIPTION_LIMIT = 139
RACE_TIMEOUT = 20
CREATE_TIMEOUT = 5
RERUN_LABEL = 're-run'
TASK_TAKEN_FMT = 'Taken by {runner_id} on {date}'
STALE_TASK_EXTRA_TIME = 300


logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class TaskAlreadyTaken(Exception):
    pass


class Status(object):
    @classmethod
    def create(cls, repo, pull, context, description, target_url, state):
        sha = repo.commit(pull.pull.head.sha).sha
        repo.create_status(sha, state, target_url, description, context)

        last_err = RuntimeError()
        for _ in range(CREATE_TIMEOUT):
            try:
                return cls(repo, pull, context)
            except Exception as err:
                last_err = err
                time.sleep(1)
        else:
            raise last_err

    def __init__(self, repo, pull, context):
        self.repo = repo
        self.pull = pull
        self.context = context

        for status in repo.commit(pull.pull.head.sha).statuses():
            if status.context == self.context:
                self.description = status.description
                self.target_url = status.target_url
                self.state = status.state
                break
        else:
            raise ValueError('No status with context {}'.format(context))


class Statuses(collections.Mapping):
    def __init__(self, repo, pull, contexts):
        self.repo = repo
        self.pull = pull
        self.contexts = contexts

    def __getitem__(self, key):
        try:
            return Status(self.repo, self.pull, key)
        except ValueError:
            raise KeyError(key)

    def __len__(self):
        length = 0
        for context in self.contexts:
            try:
                Status(self.repo, self.pull, context)
            except ValueError:
                pass
            else:
                length += 1

        return length

    def __iter__(self):
        for context in self.contexts:
            try:
                Status(self.repo, self.pull, context)
            except ValueError:
                pass
            else:
                yield context

    def values(self):
        ret = []
        for context in self.contexts:
            try:
                ret.append(Status(self.repo, self.pull, context))
            except ValueError:
                pass

        return ret

    def items(self):
        ret = []
        for context in self.contexts:
            try:
                ret.append((context, Status(self.repo, self.pull, context),))
            except ValueError:
                pass

        return ret


class Labels(collections.MutableSet):
    def __init__(self, pull):
        self.pull = pull

    def __contains__(self, label):
        return label in [l.name for l in self.pull.issue().labels()]

    def __iter__(self):
        for label in self.pull.issue().labels():
            yield label.name

    def __len__(self):
        return len([l for l in self.pull.issue().labels()])

    def add(self, label):
        self.pull.issue().add_labels(label)

    def discard(self, label):
        self.pull.issue().remove_label(label)


class PullRequest(object):
    def __init__(self, pull, repo):
        self.pull = pull
        self.repo = repo

    @property
    def labels(self):
        return Labels(self.pull)

    def tasks(self, tasks_config_path, job_cls):
        return Tasks(self, self.repo, tasks_config_path, job_cls)


class PullRequests(collections.Iterator):
    def __init__(self, repo):
        self.repo = repo
        self.pull_requests = self.repo.pull_requests()

    def __next__(self):
        return PullRequest(next(self.pull_requests), self.repo)


class Task(object):
    status_description = None

    def __init__(self, status, conf, job_cls):
        self.status = status
        self.repo = status.repo
        self.pull = status.pull
        self.refspec = 'pull/{}/head'.format(self.pull.pull.number)
        self.name = status.context

        self.priority = conf['priority']
        self.requires = conf['requires']
        self.job = job_cls(conf['job'],
                           (self.repo.clone_url, self.refspec))

    def dependencies_done(self):
        for dep in self.requires:
            if Status(self.repo, self.pull, dep).state != 'success':
                return False
        return True

    def can_run(self):
        if (self.status.state == 'pending' and
                self.status.description == 'unassigned'):
            return self.dependencies_done()
        return False

    def take(self, runner_id):
        status = Status(self.repo, self.pull, self.name)
        if status.state != 'pending' or status.description != 'unassigned':
            raise TaskAlreadyTaken(status.description)

        date = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        desc = TASK_TAKEN_FMT.format(runner_id=runner_id, date=date)
        logger.debug('Attempting to take task')
        Status.create(self.repo, self.pull, self.name, desc, '', 'pending')
        time.sleep(RACE_TIMEOUT)
        status = Status(self.repo, self.pull, self.name)

        if status.description != desc:
            raise TaskAlreadyTaken(status.description)

        self.status_description = desc

    def execute(self, exc_handler=None):
        depends_results = {}
        for dep in self.requires:
            status = Status(self.repo, self.pull, dep)
            depends_results[dep] = JobResult(
                status.state, status.description, status.target_url)

        try:
            result = self.job(depends_results)
        except Exception as exc:
            state = 'error'
            description = getattr(exc, 'description', '{type_}: {msg}'.format(
                type_=type(exc).__name__, msg=str(exc)))
            url = getattr(exc, 'url', '')
            if exc_handler is not None:
                exc_handler()
        else:
            state = result.state
            description = result.description
            url = result.url

        # verify that no other runner has assigned the task
        # this could happen due to race when taking the task
        # we can not completelly avoid this situation
        status = Status(self.repo, self.pull, self.name)
        if status.description != self.status_description:
            logger.info('Other runner is processing this task results will '
                        'not be reported: %s', status.description)
            raise RuntimeError(
                "Task was processed by multiple runners:\n\t{}\n\t{}\n".format(
                    status.description, self.status_description)
            )

        Status.create(self.repo, self.pull, self.name, description, url, state)


class Tasks(collections.Set, collections.Mapping):
    def __init__(self, pull, repo, tasks_config_path, job_cls):
        self.pull = pull
        self.repo = repo
        self.job_cls = job_cls
        self.tasks_conf = {}

        for changed_file in pull.pull.files():
            if changed_file.filename == tasks_config_path:
                logger.debug('Tasks file was modified in PR %d. Using tasks '
                             'file from PR.', pull.pull.number)
                ref = pull.pull.head.sha
                tasks_from_pr = True
                break
        else:
            logger.debug('Task file was not modified in PR %d. Using tasks '
                         'file from target branch', pull.pull.number)
            ref = pull.pull.base.ref
            tasks_from_pr = False

        # TODO: Use API once most of the PRs have tasks file
        # tasks_file = repo.file_contents(tasks_config_path, ref)
        # if not tasks_file:
        #     if tasks_from_pr:
        #         logger.warning('Tasks file was removed in PR %d',
        #                        pull.pull.number)
        #     else:
        #         logger.warning('Tasks file not present in target branch %s',
        #                        pull.pull.base.ref)
        # else:
        #     try:
        #         self.tasks_conf = yaml.load(
        #                             base64.b64decode(tasks_file.content)
        #                           )['jobs']
        #     except (yaml.error.YAMLError, TypeError, KeyError) as err:
        #         if tasks_from_pr:
        #             logger.warning('Failed to decode tasks file from PR %d: '
        #                            '%s', pull.pull.number, err)
        #         else:
        #             logger.warning('Failed to decode tasks file from branch '
        #                            '%s: %s', pull.pull.base.ref, err)
        tasks_file_url = (
            "https://raw.githubusercontent.com/{repo.owner.login}/{repo.name}/"
            "{ref}/{tasks_path}".format(repo=repo, ref=ref,
                                        tasks_path=tasks_config_path)
        )

        logger.debug("Retrieving tasks file %s", tasks_file_url)

        session = cachecontrol.CacheControl(requests.session(),
                                            cache=redis.Redis())
        response = session.get(tasks_file_url)
        if response.status_code == 200:
            try:
                self.tasks_conf = yaml.load(response.content)['jobs']
            except (yaml.error.YAMLError, TypeError, KeyError) as err:
                if tasks_from_pr:
                    logger.warning('Failed to decode tasks file from PR %d: '
                                   '%s', pull.pull.number, err)
                else:
                    logger.warning('Failed to decode tasks file from branch '
                                   '%s: %s', pull.pull.base.ref, err)
        else:
            if tasks_from_pr:
                logger.warning('Tasks file was removed in PR %d',
                               pull.pull.number)
            else:
                logger.warning('Tasks file not present in target branch %s',
                               pull.pull.base.ref)

    def __len__(self):
        return len(Statuses(self.repo, self.pull, self.tasks_conf.keys()))

    def __contains__(self, context):
        try:
            self[context]
        except KeyError:
            return False
        else:
            return True

    def __iter__(self):
        for task in self.tasks_conf:
            try:
                status = Status(self.repo, self.pull, task)
            except ValueError:
                # ignore missing
                pass
            else:
                yield Task(status, self.tasks_conf[task], self.job_cls)

    def __getitem__(self, context):
        try:
            status = Status(self.repo, self.pull, context)
        except ValueError:
            raise KeyError(context)
        else:
            return Task(status, self.tasks_conf[context], self.job_cls)

    def __bool__(self):
        return bool(len(self))

    def create(self):
        logger.debug("Creating tasks for PR %d", self.pull.pull.number)
        for task in self.tasks_conf:
            logger.debug("PR %d: %s", self.pull.pull.number, task)
            Status.create(self.repo, self.pull, task, 'unassigned', '',
                          'pending')
        logger.debug("Creating tasks for PR %d done.", self.pull.pull.number)


class TaskQueue(collections.Iterator):
    def __init__(self, repo, tasks_config_path, job_cls, runner_id,
                 allowed_users=[]):
        self.repo = repo
        self.job_cls = job_cls
        self.tasks_config_path = tasks_config_path
        self.allowed_users = allowed_users
        self.runner_id = runner_id

    def create_tasks_for_pulls(self):
        """
        Generate CI tasks represented by GitHub Statuses [1]

        The tasks are generated when:
        a. there's RERUN_LABEL on the PR
        b. there're no tasks yet

        [1] https://developer.github.com/v3/repos/statuses/
        """
        for pull in PullRequests(self.repo):
            logger.debug("PR %d", pull.pull.number)
            tasks = pull.tasks(self.tasks_config_path, self.job_cls)
            if not tasks and pull.pull.user.login in self.allowed_users:
                logger.debug('Creating tasks for PR %d', pull.pull.number)
                tasks.create()
            elif RERUN_LABEL in pull.labels:
                logger.debug('Recreating tasks for PR %d', pull.pull.number)
                pull.labels.discard(RERUN_LABEL)
                tasks.create()
            else:
                # check for stale tasks and recreate them
                now = datetime.datetime.now(pytz.UTC)
                for task in tasks:
                    timeout = datetime.timedelta(seconds=task.job.timeout)
                    if not timeout:
                        continue

                    res = parse.parse(TASK_TAKEN_FMT, task.status.description)
                    if not res:
                        continue

                    taken_on = dateutil.parser.parse(res['date'])
                    extra = datetime.timedelta(seconds=STALE_TASK_EXTRA_TIME)
                    deadline = taken_on + timeout + extra
                    if deadline > now:
                        continue

                    taken_by = res['runner_id']
                    logger.debug("Task %s on PR %d is stale, recreating. Was "
                                 "taken on %s by %s timeout %ds.", task.name,
                                 task.pull.pull.number, taken_on, taken_by,
                                 timeout)
                    Status.create(task.repo, task.pull, task.name,
                                  'unassigned', '', 'pending')

    def __next__(self):
        """
        Return next task for processing

        From tasks that are available for execution the one with maximum
        priority is returned.

        The priority is tuple (boolean, int, int)
        First member True if PR has 'prioritize' label, False otherwise
        Second member is task priority from tasks configuration file
        Third member is number of tasks on the same PR that are finished or run

        This strategy should prefer completion of PR testing over starting
        testing of new PR.
        """
        tasks_done_per_pr = {}

        tasks = []
        for pull in PullRequests(self.repo):
            tasks_done_per_pr[pull.pull.number] = 0
            for task in pull.tasks(self.tasks_config_path, self.job_cls):
                if (task.status.state != 'pending' or
                        task.status.description != 'unassigned'):
                    # the tasks is assigned or done (with whatever result)
                    tasks_done_per_pr[pull.pull.number] += 1
                if task.can_run():
                    tasks.append(task)

        for task in sorted(
            tasks,
            reverse=True,
            key=lambda t: ('prioritize' in t.pull.labels, t.priority,
                           tasks_done_per_pr[t.pull.pull.number]),
        ):
            try:
                task.take(self.runner_id)
            except TaskAlreadyTaken:
                continue
            else:
                return task
        else:
            raise StopIteration()


class JobResult(object):
    valid_states = ('success', 'error', 'failure',)

    def __init__(self, state, description=None, url=None):
        if state not in self.valid_states:
            raise ValueError('invalid state: {}'.format(state))
        self.state = state
        self.description = description[:GITHUB_DESCRIPTION_LIMIT]
        self.url = url


class AbstractJob(collections.Callable):
    __metaclass__ = abc.ABCMeta

    def __init__(self, job, build_target):
        """
        @param job - job specific data from task definition
        @param build_target - tuple of git repo url and refspec
                              git clone build_target[0]
                              git fetch origin build:build_target[1]
                              git checkout build
        """
        self.job = job
        self.target = build_target

    @property
    def timeout(self):
        # by default no timeout
        return 0

    @abc.abstractmethod
    def __call__(self, depends_results=None):
        """
        @param depends_results - dict of task_name: JobResult
        @returns JobResult instance
        """
        return JobResult('success', 'description', 'url')
