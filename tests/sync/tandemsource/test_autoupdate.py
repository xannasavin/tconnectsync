#!/usr/bin/env python3
"""
Regression tests for negative-sleep crash in TandemSourceAutoupdate.

When the pump's reported maxDateWithEvents is interpreted as being in the
future (e.g. timezone mismatch where arrow tags a local-time string as UTC),
`now - last_max_date_with_events` produced a negative value that landed in
the rolling-average list, which in turn fed `time.sleep()` and crashed the
process with `ValueError: sleep length must be non-negative`.
"""

import unittest
from unittest.mock import patch

import arrow
import requests

from tconnectsync.api.common import ApiException, ApiLoginException
from tconnectsync.sync.tandemsource.autoupdate import TandemSourceAutoupdate

from ...secrets import build_secrets


class _FakeTConnect:
    pass


class _FakeNightscout:
    pass


class TestAutoupdateNegativeSleep(unittest.TestCase):
    def setUp(self):
        self.secret = build_secrets(
            AUTOUPDATE_DEFAULT_SLEEP_SECONDS=300,
            AUTOUPDATE_MAX_SLEEP_SECONDS=1500,
            AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS=60,
            AUTOUPDATE_USE_FIXED_SLEEP=0,
            AUTOUPDATE_MAX_LOOP_INVOCATIONS=1,
            AUTOUPDATE_NO_DATA_FAILURE_MINUTES=180,
            AUTOUPDATE_FAILURE_MINUTES=75,
            AUTOUPDATE_RESTART_ON_FAILURE=False,
        )

    def _run_one_iteration(self, autoupdate, future_offset_seconds=None, max_date_iso=None):
        """Drive one autoupdate loop iteration. Either pass `future_offset_seconds`
        (produces a UTC-tagged ISO string `future_offset_seconds` ahead of now) or
        pass `max_date_iso` directly (used by tests that need a specific format,
        e.g. naive local-time strings to exercise the TIMEZONE_NAME parsing fix)."""
        if max_date_iso is None:
            assert future_offset_seconds is not None
            max_date_iso = arrow.utcnow().shift(seconds=future_offset_seconds).isoformat()
        future_iso = max_date_iso

        sleep_calls = []

        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.return_value = {
                "assignmentId": "test-device-1",
                "maxDateOfEvents": future_iso,
            }
            mock_process.return_value.process.return_value = (1, 999)

            autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)

        return sleep_calls

    def test_time_sleep_never_called_with_negative_value(self):
        """Defensive clamp: even with negative rolling-avg entries, time.sleep
        must receive a non-negative argument."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        # Simulate state after prior iterations where pump timestamps were
        # consistently ~2h in the future (TZ skew).
        autoupdate.time_diffs_between_updates = [-7200.0, -7200.0, -7200.0]
        autoupdate.last_max_date_with_events = (
            arrow.utcnow().float_timestamp + 7200
        )
        autoupdate.last_event_seqnum = 12345

        sleep_calls = self._run_one_iteration(autoupdate, future_offset_seconds=7260)

        self.assertTrue(sleep_calls, "Expected at least one time.sleep call")
        for call_arg in sleep_calls:
            self.assertGreaterEqual(
                call_arg, 0,
                "time.sleep was called with negative value %r" % call_arg,
            )

    def test_negative_diff_not_recorded_in_rolling_average(self):
        """Root cause: a negative `now - last_max_date_with_events` indicates
        clock skew and must not be appended to the rolling-average list."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        # Previous max-date is 2h in the future, so `now - past_future = negative`.
        autoupdate.last_max_date_with_events = (
            arrow.utcnow().float_timestamp + 7200
        )
        autoupdate.last_event_seqnum = 12345

        self._run_one_iteration(autoupdate, future_offset_seconds=7260)

        for diff in autoupdate.time_diffs_between_updates:
            self.assertGreaterEqual(
                diff, 0,
                "Negative diff %r leaked into time_diffs_between_updates" % diff,
            )

    def test_positive_diff_is_still_recorded(self):
        """Sanity check: the happy path (pump timestamp in the past) still
        feeds the rolling average."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        # Previous max-date is 5min in the PAST — normal case.
        autoupdate.last_max_date_with_events = (
            arrow.utcnow().float_timestamp - 300
        )
        autoupdate.last_event_seqnum = 12345

        self._run_one_iteration(autoupdate, future_offset_seconds=60)

        self.assertEqual(
            len(autoupdate.time_diffs_between_updates), 1,
            "Expected exactly one positive diff to be recorded",
        )
        self.assertGreater(autoupdate.time_diffs_between_updates[0], 0)


class TestAutoupdateNaiveTimestampParsing(unittest.TestCase):
    """Root cause regression: Tandem Source EU returns maxDateOfEvents as a
    naive ISO string in the pump's local timezone (no offset marker). Before
    the fix, arrow.get() defaulted naive strings to UTC, shifting the timestamp
    into the future of `now` by the local UTC offset and producing chronic
    negative time diffs (every cycle in production logs from 2026-05-19/20).

    Parsing now routes through the API layer's naive_local_to_utc(), which
    applies tzinfo=TIMEZONE_NAME only when the string carries no offset marker.
    Strings with an embedded offset (Z, +HH, +HHMM, +HH:MM) are honored as-is.

    Note that naive_local_to_utc() reads the module-level TIMEZONE_NAME rather
    than the secret object passed to TandemSourceAutoupdate, so these tests
    patch the constant where the function looks it up. Both resolve to the same
    env var in production."""

    def test_naive_local_time_string_parsed_in_configured_tz(self):
        secret = build_secrets(
            TIMEZONE_NAME="Europe/Berlin",
            AUTOUPDATE_DEFAULT_SLEEP_SECONDS=300,
            AUTOUPDATE_MAX_SLEEP_SECONDS=1500,
            AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS=60,
            AUTOUPDATE_USE_FIXED_SLEEP=0,
            AUTOUPDATE_MAX_LOOP_INVOCATIONS=1,
            AUTOUPDATE_NO_DATA_FAILURE_MINUTES=180,
            AUTOUPDATE_FAILURE_MINUTES=75,
            AUTOUPDATE_RESTART_ON_FAILURE=False,
        )
        autoupdate = TandemSourceAutoupdate(secret)

        # Simulate the production scenario: pump reports its local wall-clock
        # time as a naive ISO string with no offset marker.
        now_berlin = arrow.now("Europe/Berlin")
        naive_local_iso = now_berlin.format("YYYY-MM-DDTHH:mm:ss")
        self.assertNotIn("+", naive_local_iso, "fixture must be naive (no TZ)")
        self.assertNotIn("Z", naive_local_iso, "fixture must be naive (no TZ)")

        sleep_calls = []
        with patch(
            "tconnectsync.api.tandemsource.TIMEZONE_NAME", "Europe/Berlin"
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.return_value = {
                "assignmentId": "test-device-1",
                "maxDateOfEvents": naive_local_iso,
            }
            mock_process.return_value.process.return_value = (1, 999)

            autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)

        # After the fix, the parsed epoch should match wall-clock now (give or
        # take a second for test execution), NOT now + UTC_offset.
        recorded_epoch = autoupdate.last_max_date_with_events
        wall_clock_epoch = arrow.utcnow().float_timestamp
        delta = abs(recorded_epoch - wall_clock_epoch)
        self.assertLess(
            delta, 10,
            "Naive local-time string was misinterpreted as UTC (delta=%0.1fs). "
            "Expected parser to honor TIMEZONE_NAME=Europe/Berlin." % delta,
        )

    def test_embedded_tz_marker_still_honored(self):
        """A maxDateWithEvents that DOES carry an offset (e.g. US fixtures,
        future format changes) must still parse correctly even with a
        mismatching TIMEZONE_NAME, because the helper short-circuits to
        plain arrow.get() when an offset is present."""
        secret = build_secrets(
            TIMEZONE_NAME="Europe/Berlin",  # deliberately wrong for the fixture
            AUTOUPDATE_DEFAULT_SLEEP_SECONDS=300,
            AUTOUPDATE_MAX_SLEEP_SECONDS=1500,
            AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS=60,
            AUTOUPDATE_USE_FIXED_SLEEP=0,
            AUTOUPDATE_MAX_LOOP_INVOCATIONS=1,
            AUTOUPDATE_NO_DATA_FAILURE_MINUTES=180,
            AUTOUPDATE_FAILURE_MINUTES=75,
            AUTOUPDATE_RESTART_ON_FAILURE=False,
        )
        autoupdate = TandemSourceAutoupdate(secret)

        # Pump in US Eastern reports with explicit -05:00 / -04:00 offset,
        # like the existing test_process.py fixture.
        now_eastern = arrow.now("America/New_York")
        tz_tagged_iso = now_eastern.isoformat()
        self.assertIn(
            ":", tz_tagged_iso[-6:],
            "fixture must include an explicit TZ offset",
        )

        sleep_calls = []
        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.return_value = {
                "assignmentId": "test-device-1",
                "maxDateOfEvents": tz_tagged_iso,
            }
            mock_process.return_value.process.return_value = (1, 999)

            autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)

        recorded_epoch = autoupdate.last_max_date_with_events
        wall_clock_epoch = arrow.utcnow().float_timestamp
        delta = abs(recorded_epoch - wall_clock_epoch)
        self.assertLess(
            delta, 10,
            "Embedded TZ offset was overridden by TIMEZONE_NAME (delta=%0.1fs). "
            "Helper should short-circuit to arrow.get() when offset present." % delta,
        )


class TestAutoupdateTransientNetworkError(unittest.TestCase):
    """Regression: DNS failures and connection resets used to propagate up
    from ChooseDevice / ProcessTimeRange and exit the process, leading
    Docker/Synology to restart the container hourly and email the user.

    The fix wraps the loop body in a try/except for requests' ConnectionError,
    Timeout, ChunkedEncodingError, and RetryError; logs a warning; sleeps;
    and continues. Sustained outages still trigger the NO_DATA_FAILURE_MINUTES
    safety net (covered by other paths).

    Network errors share the incremental backoff of TestAutoupdateApiErrorBackoff
    (30s, doubling, capped at DEFAULT_SLEEP_SECONDS) rather than the flat
    DEFAULT_SLEEP_SECONDS they originally used: a 2-second DNS blip should not
    cost a 5-minute sync gap, while a real outage still settles at 5 minutes."""

    def setUp(self):
        self.secret = build_secrets(
            AUTOUPDATE_DEFAULT_SLEEP_SECONDS=300,
            AUTOUPDATE_MAX_SLEEP_SECONDS=1500,
            AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS=60,
            AUTOUPDATE_USE_FIXED_SLEEP=0,
            AUTOUPDATE_MAX_LOOP_INVOCATIONS=2,
            AUTOUPDATE_NO_DATA_FAILURE_MINUTES=180,
            AUTOUPDATE_FAILURE_MINUTES=75,
            AUTOUPDATE_RESTART_ON_FAILURE=False,
        )

    def _drive(self, autoupdate, choose_side_effect):
        """Drive autoupdate.process() with patched ChooseDevice and ProcessTimeRange.
        Returns (sleep_calls, result)."""
        sleep_calls = []
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.side_effect = choose_side_effect
            mock_process.return_value.process.return_value = (1, 999)
            result = autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)
        return sleep_calls, result, future_iso

    def test_connection_error_does_not_crash_loop(self):
        """A DNS failure on the first iteration must not exit the process;
        the loop should sleep and try again."""
        autoupdate = TandemSourceAutoupdate(self.secret)

        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        sleep_calls, result, _ = self._drive(
            autoupdate,
            choose_side_effect=[
                requests.exceptions.ConnectionError(
                    "HTTPSConnectionPool(host='source.eu.tandemdiabetes.com', port=443): "
                    "Max retries exceeded with url: /api/... "
                    "(Caused by NameResolutionError(...Temporary failure in name resolution))"
                ),
                {"assignmentId": "test-device-1", "maxDateOfEvents": future_iso},
            ],
        )

        self.assertIn(result, (0, None))
        self.assertEqual(autoupdate.autoupdate_invocations, 2)
        self.assertGreaterEqual(len(sleep_calls), 2)
        self.assertEqual(
            sleep_calls[0], 30,
            "First retry after a network blip should be the short backoff, "
            "not a flat 5-minute wait",
        )

    def test_timeout_does_not_crash_loop(self):
        autoupdate = TandemSourceAutoupdate(self.secret)
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        sleep_calls, _, _ = self._drive(
            autoupdate,
            choose_side_effect=[
                requests.exceptions.Timeout("Read timed out"),
                {"assignmentId": "x", "maxDateOfEvents": future_iso},
            ],
        )
        self.assertEqual(autoupdate.autoupdate_invocations, 2)
        self.assertGreaterEqual(len(sleep_calls), 2)

    def test_chunked_encoding_error_does_not_crash_loop(self):
        """A mid-stream disconnect during pump_events download surfaces as
        ChunkedEncodingError (subclass of RequestException, NOT ConnectionError),
        so it must be in the catch tuple explicitly."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        _, _, _ = self._drive(
            autoupdate,
            choose_side_effect=[
                requests.exceptions.ChunkedEncodingError("Connection broken"),
                {"assignmentId": "x", "maxDateOfEvents": future_iso},
            ],
        )
        self.assertEqual(autoupdate.autoupdate_invocations, 2)

    def test_retry_error_does_not_crash_loop(self):
        """urllib3 retry-budget exhaustion bubbles up as requests.RetryError,
        which is RequestException but not ConnectionError."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        _, _, _ = self._drive(
            autoupdate,
            choose_side_effect=[
                requests.exceptions.RetryError("Max retries exceeded"),
                {"assignmentId": "x", "maxDateOfEvents": future_iso},
            ],
        )
        self.assertEqual(autoupdate.autoupdate_invocations, 2)

    def test_non_network_exception_still_propagates(self):
        """Programming bugs (e.g. KeyError) must NOT be swallowed by the
        network-error handler — they should still crash so they get noticed."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ):
            mock_choose.return_value.choose.side_effect = KeyError("simulated bug")
            with self.assertRaises(KeyError):
                autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)

    def test_max_loop_invocations_respected_on_persistent_failure(self):
        """If the network never recovers, the loop must still terminate at
        MAX_LOOP_INVOCATIONS rather than spinning forever."""
        autoupdate = TandemSourceAutoupdate(self.secret)
        sleep_calls = []
        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ):
            mock_choose.return_value.choose.side_effect = (
                requests.exceptions.ConnectionError("dns fail")
            )
            result = autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)

        self.assertIn(result, (0, None))
        self.assertEqual(
            autoupdate.autoupdate_invocations,
            self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS,
        )


class TestAutoupdateApiErrorBackoff(unittest.TestCase):
    """Regression: on 2026-07-16 Tandem retired the reportsfacade endpoints in
    the EU region, so pump_event_metadata() began returning HTTP 404. get()
    only retries 401 and 500, so the ApiException propagated out of the loop
    and exited the process. Docker restarted the container roughly every two
    minutes, and because the credentials cache is lost on restart, EVERY
    restart performed a fresh login against sso.tandemdiabetes.com — hundreds
    of logins per hour from one IP, which risks a WAF ban.

    The fix keeps API errors inside the loop and backs off incrementally
    (30s, 60s, 120s, ... capped at AUTOUPDATE_DEFAULT_SLEEP_SECONDS) so the
    process stays alive, the credentials cache stays warm, and a sustained
    outage settles into one quiet poll every 5 minutes."""

    def setUp(self):
        self.secret = build_secrets(
            AUTOUPDATE_DEFAULT_SLEEP_SECONDS=300,
            AUTOUPDATE_MAX_SLEEP_SECONDS=1500,
            AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS=60,
            AUTOUPDATE_USE_FIXED_SLEEP=0,
            AUTOUPDATE_MAX_LOOP_INVOCATIONS=6,
            AUTOUPDATE_NO_DATA_FAILURE_MINUTES=180,
            AUTOUPDATE_FAILURE_MINUTES=75,
            AUTOUPDATE_RESTART_ON_FAILURE=False,
        )

    def _drive(self, autoupdate, choose_side_effect):
        sleep_calls = []
        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.side_effect = choose_side_effect
            mock_process.return_value.process.return_value = (1, 999)
            result = autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)
        return sleep_calls, result

    def test_api_exception_does_not_crash_loop(self):
        """The production symptom: HTTP 404 from pumpeventmetadata must be
        survivable, not fatal."""
        # One failure + one success, so stop the loop after two invocations
        # rather than running past the fixtures.
        self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS = 2
        autoupdate = TandemSourceAutoupdate(self.secret)
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()

        sleep_calls, result = self._drive(
            autoupdate,
            choose_side_effect=[
                ApiException(404, "TandemSourceApi HTTP 404 response: "),
                {"assignmentId": "test-device-1", "maxDateOfEvents": future_iso},
            ],
        )

        self.assertIn(result, (0, None))
        self.assertGreaterEqual(len(sleep_calls), 2)

    def test_backoff_grows_incrementally_and_caps_at_default_sleep(self):
        """A persistent outage must not poll at a fixed fast rate. Waits grow
        30 -> 60 -> 120 -> 240 and then hold at AUTOUPDATE_DEFAULT_SLEEP_SECONDS
        (300s = 5 minutes), never above it."""
        autoupdate = TandemSourceAutoupdate(self.secret)

        sleep_calls, _ = self._drive(
            autoupdate,
            choose_side_effect=ApiException(404, "TandemSourceApi HTTP 404 response: "),
        )

        self.assertEqual(sleep_calls, [30, 60, 120, 240, 300, 300])

    def test_backoff_resets_after_successful_iteration(self):
        """A single blip must not permanently penalize the poll rate: once a
        poll succeeds, the next failure starts again at the shortest wait."""
        # Four fixtures below, so stop after four invocations.
        self.secret.AUTOUPDATE_MAX_LOOP_INVOCATIONS = 4
        autoupdate = TandemSourceAutoupdate(self.secret)
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        device = {"assignmentId": "test-device-1", "maxDateOfEvents": future_iso}

        sleep_calls, _ = self._drive(
            autoupdate,
            choose_side_effect=[
                ApiException(502, "TandemSourceApi HTTP 502 response: "),
                ApiException(502, "TandemSourceApi HTTP 502 response: "),
                device,
                ApiException(502, "TandemSourceApi HTTP 502 response: "),
            ],
        )

        # Expected: 30 and 60 for the two failures, then the normal poll
        # interval for the successful iteration, then back to 30 — not 120 —
        # because the success reset the counter.
        self.assertEqual(
            sleep_calls[:2], [30, 60],
            "Expected the first outage to back off 30 then 60, got %r" % sleep_calls,
        )
        self.assertEqual(
            sleep_calls[-1], 30,
            "Backoff must reset to 30s after the successful poll in between, "
            "got %r (full sequence: %r)" % (sleep_calls[-1], sleep_calls),
        )

    def test_login_exception_still_propagates(self):
        """Guard: a credentials failure is NOT transient. Retrying it in-process
        would hammer the login endpoint with doomed attempts, which is exactly
        the ban risk this backoff exists to avoid. It must stay fatal so the
        user notices and fixes their config."""
        autoupdate = TandemSourceAutoupdate(self.secret)

        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ):
            mock_choose.return_value.choose.side_effect = ApiLoginException(
                401, "Invalid credentials"
            )
            with self.assertRaises(ApiLoginException):
                autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)


class TestAutoupdateSustainedFailureExit(unittest.TestCase):
    """Staying alive through an outage costs the only alarm this deployment
    has: Synology's Container Manager mails on container exit, and nothing
    watches the log stream. With the backoff swallowing API errors forever, a
    real outage (like the 2026-07-16 EU cutover) would now be silent.

    So a sustained failure escalates one final step: after
    AUTOUPDATE_API_FAILURE_MINUTES of unbroken failure, exit non-zero. Docker
    restarts, Synology sends exactly one mail per outage-hour instead of one
    per two minutes. Short blips stay silent, which is the whole point.

    This is deliberately NOT gated on AUTOUPDATE_RESTART_ON_FAILURE: that flag
    covers the pump-not-uploading watchdog, where restarting fixes nothing.
    A dead API is a different failure and deserves its own knob."""

    def _secret(self, **overrides):
        base = dict(
            AUTOUPDATE_DEFAULT_SLEEP_SECONDS=300,
            AUTOUPDATE_MAX_SLEEP_SECONDS=1500,
            AUTOUPDATE_UNEXPECTED_NO_INDEX_SLEEP_SECONDS=60,
            AUTOUPDATE_USE_FIXED_SLEEP=0,
            AUTOUPDATE_MAX_LOOP_INVOCATIONS=50,
            AUTOUPDATE_NO_DATA_FAILURE_MINUTES=180,
            AUTOUPDATE_FAILURE_MINUTES=75,
            AUTOUPDATE_RESTART_ON_FAILURE=False,
            AUTOUPDATE_API_FAILURE_MINUTES=45,
        )
        base.update(overrides)
        return build_secrets(**base)

    def _drive_with_clock(self, autoupdate, choose_side_effect):
        """Drive the loop with a fake clock that advances by each sleep, so
        simulated wall-clock time passes without the test actually waiting."""
        clock = [10_000.0]
        sleeps = []

        def fake_sleep(secs):
            sleeps.append(secs)
            clock[0] += secs

        with patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=fake_sleep,
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.time.time",
            side_effect=lambda: clock[0],
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.side_effect = choose_side_effect
            mock_process.return_value.process.return_value = (1, 999)
            result = autoupdate.process(_FakeTConnect(), _FakeNightscout(), pretend=False)

        return result, sleeps, clock[0] - 10_000.0

    def test_exits_nonzero_after_sustained_api_failure(self):
        """The production scenario: a dead endpoint. After 45 simulated minutes
        of unbroken 404s the process must exit non-zero so the platform mails."""
        autoupdate = TandemSourceAutoupdate(self._secret())

        result, sleeps, elapsed = self._drive_with_clock(
            autoupdate,
            choose_side_effect=ApiException(404, "TandemSourceApi HTTP 404 response: "),
        )

        self.assertEqual(result, 1, "Expected a non-zero exit after a sustained outage")
        self.assertGreaterEqual(
            elapsed, 45 * 60,
            "Exited after only %0.0fs; must persist a full AUTOUPDATE_API_FAILURE_MINUTES "
            "before giving up" % elapsed,
        )
        self.assertLess(
            elapsed, 75 * 60,
            "Took %0.0fs to give up; backoff should reach the threshold promptly "
            "once capped" % elapsed,
        )

    def test_recovery_before_threshold_does_not_exit(self):
        """A 10-minute outage that recovers must not trigger a mail."""
        autoupdate = TandemSourceAutoupdate(self._secret(AUTOUPDATE_MAX_LOOP_INVOCATIONS=6))
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        device = {"assignmentId": "x", "maxDateOfEvents": future_iso}

        result, _, _ = self._drive_with_clock(
            autoupdate,
            choose_side_effect=[
                ApiException(503, "down"),
                ApiException(503, "down"),
                ApiException(503, "down"),
                device,
                device,
                device,
            ],
        )

        self.assertIn(result, (0, None), "A recovered outage must not exit non-zero")

    def test_failure_clock_resets_on_success(self):
        """Two separate short outages must not add up to an exit: the failure
        clock restarts from the successful poll between them."""
        autoupdate = TandemSourceAutoupdate(self._secret(AUTOUPDATE_MAX_LOOP_INVOCATIONS=12))
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        device = {"assignmentId": "x", "maxDateOfEvents": future_iso}

        result, _, _ = self._drive_with_clock(
            autoupdate,
            choose_side_effect=[
                ApiException(503, "down"), ApiException(503, "down"),
                ApiException(503, "down"), ApiException(503, "down"),
                ApiException(503, "down"),
                device,
                ApiException(503, "down"), ApiException(503, "down"),
                ApiException(503, "down"), ApiException(503, "down"),
                ApiException(503, "down"), device,
            ],
        )

        self.assertIn(
            result, (0, None),
            "Two short outages separated by a success must not accumulate into an exit",
        )

    def test_zero_minutes_disables_the_exit(self):
        """Opt-out: 0 means never give up, for users who would rather have a
        silent process than a restarting one."""
        autoupdate = TandemSourceAutoupdate(
            self._secret(AUTOUPDATE_API_FAILURE_MINUTES=0, AUTOUPDATE_MAX_LOOP_INVOCATIONS=30)
        )

        result, _, elapsed = self._drive_with_clock(
            autoupdate,
            choose_side_effect=ApiException(404, "gone"),
        )

        self.assertIn(result, (0, None), "0 must disable the sustained-failure exit")
        self.assertGreater(
            elapsed, 45 * 60,
            "Test must simulate past the default threshold to prove it is ignored",
        )


if __name__ == "__main__":
    unittest.main()
