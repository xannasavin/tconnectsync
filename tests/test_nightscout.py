#!/usr/bin/env python3
"""Tests for the Nightscout date-filter query building in time_range()."""

import unittest
import urllib.parse

import arrow

from tconnectsync.nightscout import time_range


class TestTimeRangeEncoding(unittest.TestCase):
    """A positive UTC offset ends an ISO-8601 timestamp with '+02:00'. Placed
    raw into a query string, the '+' is a reserved character that servers decode
    as a space, so Nightscout receives '2026-07-16T00:00:00 02:00' and rejects
    it with "could not parse as a valid ISO-8601 date". Percent-encoding the
    value keeps the offset intact."""

    def test_positive_offset_is_percent_encoded(self):
        start = arrow.get("2026-07-16T00:00:00+02:00")

        arg = time_range('created_at', start, None)

        self.assertNotIn(
            '+', arg,
            "A raw '+' in the query string is decoded to a space by the server, "
            "mangling the timestamp. Got: %s" % arg,
        )
        self.assertIn('%2B', arg, "Expected the offset '+' to be encoded as %%2B. Got: %s" % arg)

    def test_encoded_value_round_trips_to_the_original_timestamp(self):
        """Decoding the query the way a server would must yield the timestamp
        we meant to send."""
        start = arrow.get("2026-07-16T00:00:00+02:00")

        arg = time_range('created_at', start, None)

        value = arg.split('=', 1)[1]
        self.assertEqual(
            arrow.get(urllib.parse.unquote(value)), start,
            "Round-tripping the encoded filter must reproduce the original instant",
        )

    def test_negative_offset_and_z_suffix_still_parse(self):
        """US pumps report a '-04:00' offset and UTC values end in 'Z'; neither
        is ambiguous in a query string, but both must survive encoding."""
        for iso in ("2026-07-16T00:00:00-04:00", "2026-07-16T00:00:00Z"):
            with self.subTest(iso=iso):
                expected = arrow.get(iso)
                arg = time_range('created_at', expected, None)
                value = arg.split('=', 1)[1]
                self.assertEqual(arrow.get(urllib.parse.unquote(value)), expected)

    def test_both_bounds_are_emitted(self):
        start = arrow.get("2026-07-16T00:00:00+02:00")
        end = arrow.get("2026-07-17T00:00:00+02:00")

        arg = time_range('created_at', start, end)

        self.assertIn('find[created_at][$gte]=', arg)
        self.assertIn('find[created_at][$lte]=', arg)

    def test_omitted_bounds_produce_no_filter(self):
        self.assertEqual(time_range('created_at', None, None), '')


if __name__ == "__main__":
    unittest.main()
