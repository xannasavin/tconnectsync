import time
import logging
import datetime
import sys
import arrow
import requests

from ...api.common import ApiException, ApiLoginException
from ...features import DEFAULT_FEATURES
from ...api.tandemsource import naive_local_to_utc
from .process import ProcessTimeRange
from .choose_device import ChooseDevice

logger = logging.getLogger(__name__)

# Shortest wait after a failed poll. Doubles per consecutive failure, capped at
# AUTOUPDATE_DEFAULT_SLEEP_SECONDS (5 min by default): 30, 60, 120, 240, 300...
RETRY_INITIAL_SLEEP_SECONDS = 30

# Consecutive failures before the retry log line escalates from WARNING to
# ERROR, so a sustained outage doesn't hide quietly inside the backoff.
RETRY_ESCALATE_AFTER_FAILURES = 3

class TandemSourceAutoupdate:
    """Wrap access to secrets for easier testing."""
    def __init__(self, secret):
        self.secret = secret
        self.autoupdate_invocations = 0
        self.consecutive_failures = 0
        self.last_max_date_with_events = None
        self.last_event_time = 0
        self.last_attempt_time = 0
        self.last_event_seqnum = None
        self.last_successful_process_time_range = None
        self.time_diffs_between_attempts = []
        self.time_diffs_between_updates = []

    """
    Performs the auto-update functionality. Runs indefinitely in a loop
    until stopped (ctrl+c), or a maximum of AUTOUPDATE_MAX_LOOP_INVOCATIONS times.
    Stops if AUTOUPDATE_RESTART_ON_FAILURE is set and an error occurs.
    """
    def process(self, tconnect, nightscout, pretend, features=None):
        if features is None:
            features = DEFAULT_FEATURES

        # Query for data, find exact interval to cut down on API calls
        # Refresh API token. If failure, die, have wrapper script re-run.

        self.autoupdate_start = time.time()

        while True:
            try:
                logger.debug("autoupdate loop")
                now = time.time()

                time_end = datetime.datetime.now()
                time_start = time_end - datetime.timedelta(days=1)

                tconnectDevice = ChooseDevice(self.secret, tconnect).choose()

                event_seqnum = None
                cur_max_date_with_events = arrow.get(naive_local_to_utc(tconnectDevice['maxDateOfEvents'])).float_timestamp
                if not self.last_max_date_with_events or cur_max_date_with_events > self.last_max_date_with_events:
                    logger.info('New reported tandemsource data. (cur_max_date: %s last_max_date: %s)' % (cur_max_date_with_events, self.last_max_date_with_events))

                    if pretend:
                        logger.info('Would update now if not in pretend mode')
                    else:
                        added, event_seqnum = ProcessTimeRange(tconnect, nightscout, tconnectDevice, pretend, self.secret, features=features).process(time_start, time_end)
                        logger.info('Added %d items from ProcessTimeRange' % added)
                        self.last_successful_process_time_range = now

                    # Track the time it took to find a new event between runs,
                    # but skip this calculation the first process cycle (since
                    # we don't know at what exact point the event index changed)
                    if self.last_event_seqnum:
                        # A negative diff means the pump's previously-reported maxDateWithEvents
                        # was in the future of wall-clock `now` — almost always a timezone /
                        # clock-skew issue (e.g. pump timestamps tagged as UTC but actually
                        # local time). Recording it would poison the rolling average and
                        # eventually produce a negative sleep_secs that crashes time.sleep().
                        diff = now - self.last_max_date_with_events
                        if diff >= 0:
                            self.time_diffs_between_updates.append(diff)
                            logger.debug('Updating tracking of time since last update: %s' % self.time_diffs_between_updates)
                        else:
                            logger.warning(
                                'Skipping negative time diff (%0.1fs) — likely pump clock skew or timezone mismatch' % diff
                            )

                    # Mark the last event index uploaded from the pump and timestamp
                    if event_seqnum:
                        self.last_event_seqnum = event_seqnum
                        self.last_event_time = now
                    self.last_max_date_with_events = cur_max_date_with_events
                    self.last_attempt_time = now
                    self.time_diffs_between_attempts = []
                else:
                    logger.info('No new reported tandemsource data. cur_max_date: %s (%s) last_event_time: %s (%s)' % (
                        arrow.get(cur_max_date_with_events) if cur_max_date_with_events else None,
                        '%dm ago' % ((now - cur_max_date_with_events)//60) if cur_max_date_with_events else None,
                        arrow.get(self.last_event_time) if self.last_event_time else None,
                        '%dm ago' % ((now - self.last_event_time)//60) if self.last_event_time else None
                    ))

                    # If we haven't seen the pump event index update in AUTOUPDATE_NO_DATA_FAILURE_MINUTES,
                    # then trigger an error and potentially restart.
                    # The most likely case here is that the pump isn't uploading right now.
                    if self.last_event_time and (now - self.last_event_time) >= 60 * self.secret.AUTOUPDATE_NO_DATA_FAILURE_MINUTES:
                        logger.error(AutoupdateNoEventIndexesDetectedError(
                            "%s: No new data event indexes have been detected for %d minutes. " % (datetime.datetime.now(), (now - self.last_event_time)//60) +
                            "New data might not be uploading."))

                        # TODO: restarting doesn't really help anything here.
                        # Should we notify the user?
                        if self.secret.AUTOUPDATE_RESTART_ON_FAILURE:
                            logger.error("Exiting with error code due to AUTOUPDATE_RESTART_ON_FAILURE")
                            return 1

                    # Similarly, if we HAVE seen pump event indexes update but have not successfully
                    # found any associated data updates from the tconnect API for AUTOUPDATE_NO_DATA_FAILURE_MINUTES,
                    # trigger an error and potentially restart. This could either be a tconnectsync problem,
                    # where we can see the indexes increasing, but it takes us until a period of no index
                    # update to reach our AUTOUPDATE_FAILURE_MINUTES threshold; or, a side effect of the
                    # above no indexes warning.
                    elif self.last_successful_process_time_range and (now - self.last_successful_process_time_range) >= 60 * self.secret.AUTOUPDATE_FAILURE_MINUTES:
                        logger.error(AutoupdateNoNewDataDetectedError(
                            "%s: No new data has been detected via the API for %d minutes (last: %s). " % (datetime.datetime.now(), (now - self.last_successful_process_time_range)//60, self.last_successful_process_time_range) +
                            "tconnectsync might not be functioning properly."))

                        if self.secret.AUTOUPDATE_RESTART_ON_FAILURE:
                            logger.error("%s: Exiting with error code due to AUTOUPDATE_RESTART_ON_FAILURE" % datetime.datetime.now())
                            return 1

                    # Track how long we've been retrying
                    if self.last_attempt_time:
                        self.time_diffs_between_attempts.append(now - self.last_attempt_time)

                    self.last_attempt_time = now

                    # If it's been 3 loops since the last time we found new data,
                    # then we're not in sync with the rate at which pump data is being
                    # uploaded, so
                    if len(self.time_diffs_between_attempts) >= 3:
                        # The pump hasn't sent us data that, based on previous cadence, we were expecting
                        logger.warning(AutoupdateNoIndexChangeWarning("Sleeping %d seconds after unexpected no index change based on previous cadence. (New data might be delayed.)" %
                            int(self.secret.AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS)))

                        logger.debug("Last event time: %s, time diffs between attempts: %s" % (self.last_event_time, self.time_diffs_between_attempts))

                        # The API answered, so any prior outage is over.
                        self.consecutive_failures = 0

                        time.sleep(self.secret.AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS)

                        # Since we bail early, update the invocations count and potentially exit after sleeping.
                        self.autoupdate_invocations += 1
                        if self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS > 0 and self.autoupdate_invocations >= self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS:
                            return 0

                        continue

                # The API answered, so any prior outage is over.
                self.consecutive_failures = 0

                sleep_secs = self.secret.AUTOUPDATE_DEFAULT_SLEEP_SECONDS

                # Sleep for a rolling average of time between updates
                if self.secret.AUTOUPDATE_USE_FIXED_SLEEP != 1:
                    logger.debug("Time diffs between updates: %s" % self.time_diffs_between_updates)

                    # Only keep the 10 latest time diffs
                    if len(self.time_diffs_between_updates) > 10:
                        self.time_diffs_between_updates = self.time_diffs_between_updates[1:]

                    # If we have less than 3 data points,
                    if len(self.time_diffs_between_updates) > 2:
                        sleep_secs = sum(self.time_diffs_between_updates) / len(self.time_diffs_between_updates)

                    # At minimum, update every AUTOUPDATE_MAX_SLEEP_SECONDS regardless
                    # of how often we're seeing new data appear
                    if sleep_secs > self.secret.AUTOUPDATE_MAX_SLEEP_SECONDS:
                        sleep_secs = self.secret.AUTOUPDATE_MAX_SLEEP_SECONDS

                # Defensive: with the negative-diff filter above, sleep_secs should never be
                # negative, but legacy state from before the fix or other unexpected inputs
                # could still produce one. Clamp to AUTOUPDATE_DEFAULT_SLEEP_SECONDS so we
                # don't crash with ValueError nor tight-loop the API.
                if sleep_secs < 0:
                    logger.warning(
                        'Computed negative sleep duration (%0.1fs), falling back to default %ds' % (
                            sleep_secs, self.secret.AUTOUPDATE_DEFAULT_SLEEP_SECONDS
                        )
                    )
                    sleep_secs = self.secret.AUTOUPDATE_DEFAULT_SLEEP_SECONDS

                logger.info('Sleeping for %0.01f sec' % sleep_secs)
                time.sleep(sleep_secs)

                self.autoupdate_invocations += 1
                if self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS > 0 and self.autoupdate_invocations >= self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS:
                    return 0

            except ApiLoginException:
                # A credentials failure is not transient: retrying it in-process
                # would hammer the login endpoint with attempts that cannot
                # succeed, which is the exact ban risk the backoff below exists
                # to prevent. Stay fatal so the user notices and fixes config.
                raise

            except (
                ApiException,
                requests.exceptions.ConnectionError,
                requests.exceptions.Timeout,
                requests.exceptions.ChunkedEncodingError,
                requests.exceptions.RetryError,
            ) as e:
                # Two failure families, one response. Transient network errors
                # (DNS, refused connections, timeouts, mid-stream disconnects,
                # urllib3 retry-budget exhaustion) and API errors that get()
                # does not retry itself (it only handles 401 and 500 — a 404,
                # 502 or 503 propagates) both used to exit the process and let
                # Docker restart the container.
                #
                # Restarting is the worst possible response: the credentials
                # cache dies with the process, so every restart performs a full
                # login. During the 2026-07-16 EU outage that meant a fresh
                # login every ~2 minutes for hours from a single IP. Staying in
                # the loop keeps the cache warm and the login endpoint untouched.
                self.consecutive_failures += 1
                sleep_secs = self._retry_sleep_seconds()

                log = logger.error if self.consecutive_failures >= RETRY_ESCALATE_AFTER_FAILURES else logger.warning
                log(
                    'Error during autoupdate poll (%d consecutive): %s. Sleeping %ds before retry.' % (
                        self.consecutive_failures, e, sleep_secs
                    )
                )

                time.sleep(sleep_secs)
                self.autoupdate_invocations += 1
                if self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS > 0 and self.autoupdate_invocations >= self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS:
                    return 0

    def _retry_sleep_seconds(self):
        """Exponential backoff for consecutive failed polls: 30, 60, 120, 240,
        then held at AUTOUPDATE_DEFAULT_SLEEP_SECONDS (300s default). The cap
        reuses the existing poll interval because a failing API should never be
        contacted more often than a healthy one."""
        backoff = RETRY_INITIAL_SLEEP_SECONDS * (2 ** (self.consecutive_failures - 1))
        return min(backoff, self.secret.AUTOUPDATE_DEFAULT_SLEEP_SECONDS)


class AutoupdateError(RuntimeError):
    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, super().__str__())

class AutoupdateWarning(RuntimeWarning):
    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, super().__str__())
class AutoupdateFailureError(AutoupdateError):
    pass

class AutoupdateFailureWarning(AutoupdateWarning):
    pass

class AutoupdateNoEventIndexesDetectedError(AutoupdateError):
    pass

class AutoupdateNoNewDataDetectedError(AutoupdateError):
    pass

class AutoupdateNoIndexChangeWarning(AutoupdateWarning):
    pass
