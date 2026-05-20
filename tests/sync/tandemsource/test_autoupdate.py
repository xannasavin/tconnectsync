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
                "tconnectDeviceId": "test-device-1",
                "maxDateWithEvents": future_iso,
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
    """Root cause regression: Tandem Source EU returns maxDateWithEvents as a
    naive ISO string in the pump's local timezone (no offset marker). Before
    the fix, arrow.get() defaulted naive strings to UTC, shifting the timestamp
    into the future of `now` by the local UTC offset and producing chronic
    negative time diffs (every cycle in production logs from 2026-05-19/20).

    The fix routes parsing through parse_max_date_with_events() which applies
    tzinfo=secret.TIMEZONE_NAME only when the string carries no offset marker.
    Strings with an embedded offset (Z, +HH, +HHMM, +HH:MM) are honored
    as-is."""

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
            "tconnectsync.sync.tandemsource.autoupdate.time.sleep",
            side_effect=lambda s: sleep_calls.append(s),
        ), patch(
            "tconnectsync.sync.tandemsource.autoupdate.ChooseDevice"
        ) as mock_choose, patch(
            "tconnectsync.sync.tandemsource.autoupdate.ProcessTimeRange"
        ) as mock_process:
            mock_choose.return_value.choose.return_value = {
                "tconnectDeviceId": "test-device-1",
                "maxDateWithEvents": naive_local_iso,
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
                "tconnectDeviceId": "test-device-1",
                "maxDateWithEvents": tz_tagged_iso,
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
    Timeout, ChunkedEncodingError, and RetryError; logs a warning; sleeps
    DEFAULT_SLEEP_SECONDS; and continues. Sustained outages still trigger
    the NO_DATA_FAILURE_MINUTES safety net (covered by other paths)."""

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
                {"tconnectDeviceId": "test-device-1", "maxDateWithEvents": future_iso},
            ],
        )

        self.assertIn(result, (0, None))
        self.assertEqual(autoupdate.autoupdate_invocations, 2)
        self.assertGreaterEqual(len(sleep_calls), 2)
        self.assertEqual(sleep_calls[0], self.secret.AUTOUPDATE_DEFAULT_SLEEP_SECONDS)

    def test_timeout_does_not_crash_loop(self):
        autoupdate = TandemSourceAutoupdate(self.secret)
        future_iso = arrow.utcnow().shift(seconds=60).isoformat()
        sleep_calls, _, _ = self._drive(
            autoupdate,
            choose_side_effect=[
                requests.exceptions.Timeout("Read timed out"),
                {"tconnectDeviceId": "x", "maxDateWithEvents": future_iso},
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
                {"tconnectDeviceId": "x", "maxDateWithEvents": future_iso},
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
                {"tconnectDeviceId": "x", "maxDateWithEvents": future_iso},
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


if __name__ == "__main__":
    unittest.main()
