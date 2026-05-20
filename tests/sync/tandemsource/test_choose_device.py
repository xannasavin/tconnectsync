#!/usr/bin/env python3
"""Tests for ChooseDevice and the parse_max_date_with_events helper."""

import unittest
from unittest.mock import MagicMock

from tconnectsync.sync.tandemsource.choose_device import (
    ChooseDevice,
    NoPumpsFoundError,
    parse_max_date_with_events,
)

from ...secrets import build_secrets


class TestParseMaxDateWithEvents(unittest.TestCase):
    """The parser must apply `configured_tz` to naive strings and honor any
    embedded TZ offset on tagged strings (production EU vs test/US fixtures).
    Covers all ISO-8601 offset forms reasonable APIs might emit: Z, ±HH,
    ±HHMM, ±HH:MM."""

    def test_naive_string_parsed_in_configured_tz(self):
        # "2026-05-19T11:09:18" interpreted as Europe/Berlin (CEST = UTC+2)
        result = parse_max_date_with_events(
            "2026-05-19T11:09:18", "Europe/Berlin"
        )
        self.assertEqual(result.to("UTC").format("YYYY-MM-DDTHH:mm:ss"), "2026-05-19T09:09:18")

    def test_offset_with_colon_honored(self):
        result = parse_max_date_with_events(
            "2025-11-18T13:00:00-05:00", "Europe/Berlin"
        )
        # -05:00 wins over configured Europe/Berlin
        self.assertEqual(result.to("UTC").format("YYYY-MM-DDTHH:mm:ss"), "2025-11-18T18:00:00")

    def test_offset_without_colon_honored(self):
        result = parse_max_date_with_events(
            "2025-11-18T13:00:00-0500", "Europe/Berlin"
        )
        self.assertEqual(result.to("UTC").format("YYYY-MM-DDTHH:mm:ss"), "2025-11-18T18:00:00")

    def test_bare_hour_offset_honored(self):
        """ISO-8601 permits ±HH (hours only). Arrow parses it; the regex must
        recognize it as a present offset so configured_tz does not override."""
        result = parse_max_date_with_events(
            "2025-11-18T13:00:00-05", "Europe/Berlin"
        )
        self.assertEqual(result.to("UTC").format("YYYY-MM-DDTHH:mm:ss"), "2025-11-18T18:00:00")

    def test_z_suffix_honored(self):
        result = parse_max_date_with_events(
            "2025-11-18T13:00:00Z", "Europe/Berlin"
        )
        self.assertEqual(result.to("UTC").format("YYYY-MM-DDTHH:mm:ss"), "2025-11-18T13:00:00")

    def test_naive_with_us_tz_still_works(self):
        # Regression for US users: TIMEZONE_NAME=America/New_York with a naive
        # field should be interpreted as Eastern, not UTC.
        result = parse_max_date_with_events(
            "2025-11-18T13:00:00", "America/New_York"
        )
        # 13:00 EST is 18:00 UTC; 13:00 EDT is 17:00 UTC. November = EST.
        self.assertEqual(result.to("UTC").format("YYYY-MM-DDTHH:mm:ss"), "2025-11-18T18:00:00")


class TestChooseDeviceEmptyAccount(unittest.TestCase):
    """When the account has no pumps and no PUMP_SERIAL_NUMBER is configured,
    we used to silently dereference None and crash with an opaque TypeError.
    Raise a descriptive error instead."""

    def test_empty_pump_list_raises_descriptive_error(self):
        tconnect = MagicMock()
        tconnect.tandemsource.pump_event_metadata.return_value = []
        secret = build_secrets(PUMP_SERIAL_NUMBER=None)

        with self.assertRaises(NoPumpsFoundError) as ctx:
            ChooseDevice(secret, tconnect).choose()

        msg = str(ctx.exception)
        self.assertIn("No pumps found", msg)
        self.assertIn("TCONNECT_EMAIL", msg)

    def test_empty_pump_list_with_serial_raises_invalid_serial(self):
        """If the user configured a specific serial and zero pumps come back,
        the existing InvalidSerialNumber path still wins — its message
        already tells the user to check the serial."""
        from tconnectsync.sync.tandemsource.choose_device import InvalidSerialNumber

        tconnect = MagicMock()
        tconnect.tandemsource.pump_event_metadata.return_value = []
        secret = build_secrets(PUMP_SERIAL_NUMBER="9999999")

        with self.assertRaises(InvalidSerialNumber):
            ChooseDevice(secret, tconnect).choose()


if __name__ == "__main__":
    unittest.main()
