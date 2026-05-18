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

    def _run_one_iteration(self, autoupdate, future_offset_seconds):
        """Drive one autoupdate loop iteration where the pump reports a
        maxDateWithEvents that's `future_offset_seconds` ahead of `now`."""
        future_iso = arrow.utcnow().shift(seconds=future_offset_seconds).isoformat()

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


if __name__ == "__main__":
    unittest.main()
