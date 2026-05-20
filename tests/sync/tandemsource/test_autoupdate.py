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


if __name__ == "__main__":
    unittest.main()
