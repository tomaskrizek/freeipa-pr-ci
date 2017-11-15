import datetime
import logging
import requests
import time
import ssl

from cachecontrol.adapter import CacheControlAdapter
from requests.packages.urllib3.poolmanager import PoolManager


RETRY_TIME = 1

logger = logging.getLogger(__name__)  # pylint: disable=invalid-name


class GitHubAdapter(CacheControlAdapter):
    """Handles GitHub request rate limit exhaustion and uses cachecontrol
    """
    def __init__(self, tries=3, *args, **kwargs):
        if not isinstance(tries, int) or tries <= 0:
            raise ValueError('tries must be positive integer')
        self.tries = tries

        super(GitHubAdapter, self).__init__(*args, **kwargs)

    def init_poolmanager(self, connections, maxsize, block=False):
            self.poolmanager = PoolManager(
                num_pools=connections, maxsize=maxsize,
                block=block, ssl_version=ssl.PROTOCOL_SSLv23)

    def send(self, request, *args, **kwargs):
        # Force the caching mechanism to ignore max-age and send the request
        # with ETag. This way we get the freshest data without consuming
        # rate-limited requests
        # TODO: once cachecontrol containing fix from
        # https://github.com/ionrock/cachecontrol/pull/163
        # is available everywhere we care replace with following
        # it has the same effect but more suits better semantically
        # request.headers['cache-control'] = 'max-age=0'
        request.headers['cache-control'] = 'no-cache'

        for try_counter in range(self.tries):
            logger.debug('%s: try %d', self.__class__.__name__, try_counter)

            # Rate-limit requests to avoid hitting GitHub API abuse limit
            time.sleep(0.5)

            try:
                response = super(GitHubAdapter, self).send(
                    request, *args, **kwargs)
            except requests.exceptions.ConnectionError as exc:
                logger.debug('Failed to send request: %s.', exc)
                if try_counter < (self.tries - 1):
                    logger.debug('This might be caused by temporary network '
                                 'or service issue. Will retry in %ds',
                                 RETRY_TIME)
                    time.sleep(RETRY_TIME)
                    continue
                else:
                    raise

            logger.debug('Got response: %d', response.status_code)

            try:
                rl_remaining = int(response.headers['X-RateLimit-Remaining'])
            except (ValueError, KeyError):
                rl_remaining = -1

            if response.status_code == 403 and rl_remaining == 0:
                rl_reset_ts = float(response.headers['X-RateLimit-Reset'])

                rl_reset = datetime.datetime.utcfromtimestamp(rl_reset_ts)
                now = datetime.datetime.utcfromtimestamp(time.time())

                # There's edge situation when the rate limit is reset after the
                # response was sent by server but before we have processed it
                # then the wait_time would be negative. In this situation we
                # don't want to wait
                wait_time = max(0.0, (rl_reset - now).total_seconds())

                logger.debug('Rate limit exhausted. Will wait %ds for the '
                             'limit to reset.', wait_time)
                time.sleep(wait_time)
                continue

            return response

    def build_response(self, request, response, *args, **kwargs):
        resp = super(GitHubAdapter, self).build_response(
            request, response, *args, **kwargs)

        if not resp.from_cache:
            logger.debug('%s\t%s consumed GitHub API request.',
                         request.method, request.url)

        return resp
