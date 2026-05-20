import arrow
import logging
import re

logger = logging.getLogger(__name__)

# ISO-8601 trailing offset forms: "Z", "±HH", "±HHMM", "±HH:MM".
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

class ChooseDevice:
    def __init__(self, secret, tconnect):
        self.secret = secret
        self.tconnect = tconnect

    def choose(self):
        tconnect = self.tconnect

        pumpEventMetadata = tconnect.tandemsource.pump_event_metadata()

        serialNumberToPump = {p['serialNumber']: p for p in pumpEventMetadata}
        logger.info(f'Found {len(serialNumberToPump)} pumps: {serialNumberToPump.keys()}')

        tconnectDevice = None

        if self.secret.PUMP_SERIAL_NUMBER and str(self.secret.PUMP_SERIAL_NUMBER) != '11111111':
            if not str(self.secret.PUMP_SERIAL_NUMBER) in serialNumberToPump.keys():
                raise InvalidSerialNumber(f'Serial number {self.secret.PUMP_SERIAL_NUMBER} is not present on your account: choose one of {", ".join(serialNumberToPump.keys())}')

            tconnectDevice = serialNumberToPump[str(self.secret.PUMP_SERIAL_NUMBER)]

            # Warn if pump is stale (no events in >3 days)
            try:
                max_event_date = parse_max_date_with_events(
                    tconnectDevice["maxDateWithEvents"], self.secret.TIMEZONE_NAME
                )
                age_days = (arrow.utcnow() - max_event_date).days

                if age_days > 3:
                    logger.warning(
                        f"The selected pump (serial {tconnectDevice['serialNumber']}) has no events in the last {age_days} days "
                        f"(last seen: {tconnectDevice['maxDateWithEvents']}). "
                        "You may have switched to a new pump. Consider removing or updating PUMP_SERIAL_NUMBER in your config."
                    )
            except Exception as e:
                logger.debug(f"Could not parse maxDateWithEvents to check for staleness: {e}")

            logger.info(f'Using pump with serial: {tconnectDevice["serialNumber"]} (tconnectDeviceId: {tconnectDevice["tconnectDeviceId"]}, last seen: {tconnectDevice["maxDateWithEvents"]})')
        else:
            maxDateSeen = None
            for pump in pumpEventMetadata:
                pump_max_date = parse_max_date_with_events(
                    pump['maxDateWithEvents'], self.secret.TIMEZONE_NAME
                )
                if not tconnectDevice:
                    tconnectDevice = pump
                    maxDateSeen = pump_max_date
                else:
                    if pump_max_date > maxDateSeen:
                        maxDateSeen = pump_max_date
                        tconnectDevice = pump

            if tconnectDevice is None:
                raise NoPumpsFoundError(
                    "No pumps found on this account. Check TCONNECT_EMAIL and "
                    "TCONNECT_PASSWORD, and confirm the pump has uploaded data "
                    "to Tandem Source at least once."
                )

            logger.info(f'Using most recent pump (serial: {tconnectDevice["serialNumber"]}, tconnectDeviceId: {tconnectDevice["tconnectDeviceId"]}, last seen: {tconnectDevice["maxDateWithEvents"]})')


        return tconnectDevice



class InvalidSerialNumber(RuntimeError):
    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, super().__str__())


class NoPumpsFoundError(RuntimeError):
    def __str__(self):
        return "%s: %s" % (self.__class__.__name__, super().__str__())