import re

import arrow


# ISO-8601 trailing offset forms: "Z", "+HH", "+HHMM", "+HH:MM" (or "-").
_TZ_OFFSET_RE = re.compile(r"(Z|[+-]\d{2}(:?\d{2})?)$")


def parse_max_date_with_events(value, configured_tz):
    """Parse a Tandem Source maxDateWithEvents field.

    Tandem Source EU returns naive ISO strings in the pump's local timezone
    (no offset marker), whereas test fixtures and other Tandem variants may
    carry an explicit offset. arrow.get(s, tzinfo=X) overrides any embedded
    offset, so we apply tzinfo only when the string is naive."""
    if _TZ_OFFSET_RE.search(value):
        return arrow.get(value)
    return arrow.get(value, tzinfo=configured_tz)


def insulin_float_round(amt):
    if type(amt) != float:
        return amt
    return round(amt, 2)


def insulin_milliunits_to_real(amtMilli):
    return insulin_float_round(amtMilli / 1000)
