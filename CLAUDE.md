# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`tconnectsync` is a one-way synchronizer: **Tandem Source → Nightscout**. It pulls basal, bolus, pump event, profile, and optional CGM data from Tandem's undocumented APIs and uploads it to a Nightscout instance as treatments/entries. As of v2.0+, the primary data source is Tandem Source (t:connect was shut down in the US on 2024-09-30); the older t:connect APIs remain only as fallbacks for IOB and legacy paths.

## Common Commands

Dependency installation uses **Pipenv** for development (`Pipfile`) and also supports `pip install -e .` (driven by `setup.cfg`). There is no `pyproject.toml`-based dependency list — `pyproject.toml` only declares the build backend.

```bash
# Install dev environment
pipenv install --dev

# Run the CLI (loads .env from CWD or ~/.config/tconnectsync/.env)
pipenv run tconnectsync --help
pipenv run tconnectsync --check-login
pipenv run tconnectsync --auto-update
pipenv run tconnectsync --start-date 2024-01-01 --end-date 2024-01-07
pipenv run tconnectsync --features BASAL BOLUS PUMP_EVENTS PROFILES CGM

# Test / lint (Pipfile scripts)
pipenv run test                          # python3 -m unittest discover -vv
pipenv run lint                          # flake8 syntax + complexity pass
pipenv run build_events                  # regenerate eventparser/events.py from events.json

# Pytest is also supported (CI uses both unittest and pytest)
pytest
pytest tests/sync/tandemsource/test_process.py
pytest tests/sync/tandemsource/test_process.py::TestClassName::test_method
```

CI (`.github/workflows/python-package.yml`) runs on Python 3.8–3.11 and executes: `flake8` (syntax-errors-only gate, then full warnings pass), `tconnectsync --help` smoke test, `pytest`, and `coverage run -m unittest`. Matching that order locally is the fastest way to mirror CI.

## Architecture

### Entry flow

`main.py` → `tconnectsync.main()` in `tconnectsync/__init__.py`. That function:

1. Parses CLI args and configures logging.
2. Builds a `TConnectApi` (email/password/region) and a `NightscoutApi` (URL/secret).
3. Either runs `check_login`, enters the `TandemSourceAutoupdate` loop (with `--auto-update`), or runs a single `TandemSourceProcessTimeRange(...).process(time_start, time_end)` and exits.

Auto-update polls `tandemsource.pump_events` via `ChooseDevice` → `ProcessTimeRange`, adjusting sleep intervals based on `AUTOUPDATE_*` env vars in `secret.py`.

### API layer (`tconnectsync/api/`)

`TConnectApi` is a lazy wrapper that instantiates five different API clients on demand, each with its own login and re-login logic:

- **`tandemsource.py`** — primary. Used for all event data in v2.x.
- **`controliq.py`** — legacy Control:IQ timeline. Still used in some code paths and needed to obtain `userGuid` for `ws2`.
- **`ws2.py`** — legacy t:connect web service. Slow and flaky (see issue #43); used only as a fallback for bolus/IOB.
- **`android.py`** — reverse-engineered Android app endpoints.
- **`webui.py`** — HTML scraper for the old web UI.

If you add a new endpoint, place it on the client that matches the upstream URL host, and be careful about the `needs_relogin()` / lazy-instantiation pattern — hitting a stale token triggers a full re-login.

### Sync layer (`tconnectsync/sync/tandemsource/`)

`ProcessTimeRange.process()` is the central orchestrator:

1. Calls `tandemsource.pump_events()` for the time window.
2. Groups events by `EventClass` (from `domain/tandemsource/event_class.py`).
3. For each event class, looks up a processor in `ProcessTimeRange.event_classes` (e.g. `ProcessBasal`, `ProcessBolus`, `ProcessCGMReading`, `ProcessAlarm`, `ProcessUserMode`, etc.), checks `processor.enabled()` against the current feature set, then calls `processor.process(events, ...)` → `processor.write(ns_entries)`.
4. Runs stateless updaters listed in `updater_classes` (currently just `UpdateProfiles`).
5. Returns `(processed_count, last_event_seqnum)`.

**When adding a new event type**: add the `EventClass` mapping, create a `ProcessX` module that mirrors the existing processors' interface (`enabled`, `process`, `write`), and register it in `ProcessTimeRange.event_classes`. If it maps to a new feature flag, add the constant to `features.py` and thread it through `DEFAULT_FEATURES` / `ALL_FEATURES`.

Time handling uses `arrow` throughout. `ProcessTimeRange.process()` caps `events_last_time` at `time_end` to defend against pump clock drift — preserve that behavior when editing this method.

### Feature flags (`tconnectsync/features.py`)

All synchronization is gated by feature flags. `DEFAULT_FEATURES = [BASAL, BOLUS, PUMP_EVENTS, PROFILES]`. Others (`CGM`, `IOB`, `PUMP_EVENTS_BASAL_SUSPENSION`, `CGM_ALERTS`, `DEVICE_STATUS`, plus `BOLUS_BG` gated behind `ENABLE_TESTING_MODES`) must be enabled via `--features`. Each `ProcessX.enabled()` implementation checks this list — do not bypass it.

### Event parser (`tconnectsync/eventparser/`)

`events.py` is **autogenerated** from `events.json` by `build_events.py`. The generated file has a `# THIS FILE IS AUTOGENERATED. DO NOT EDIT.` header — edits will be overwritten.

- To update for new Tandem firmware: run `scripts/sync_tandem_events.py <URL-to-reports-module-chunk.js>` to download the latest minified bundle, extract the `JSON.parse(...)` payload, update `events.json`, and regenerate `events.py`.
- `custom_events.json` holds overrides layered on top of the upstream dump.
- Runtime event decoding uses `RawEvent` / `BaseEvent` and a fixed `EVENT_LEN = 26` binary struct format.

### Configuration (`tconnectsync/secret.py`)

All config is loaded via `python-dotenv` from (in order): `./.env`, `~/.config/tconnectsync/.env`, or process env vars. `secret.py` is imported at package load — any new config value must be added there and read via the `get` / `get_bool` / `get_number` / `get_one_of` helpers so it participates in the same precedence. Do not read `os.environ` directly elsewhere in the codebase.

Notable flags consumed across the codebase: `TCONNECT_REGION` (`US`/`EU`), `PUMP_SERIAL_NUMBER` (optional — when unset, most-recently-used pump is chosen), `AUTOUPDATE_*` sleep/failure tuning, `NIGHTSCOUT_PROFILE_UPLOAD_MODE` (`add`/`replace`), `FETCH_ALL_EVENT_TYPES` (also auto-enabled when `DEVICE_STATUS` feature is on), `IGNORE_ZERO_UNIT_BASAL`, `SKIP_NS_LAST_UPLOADED_CHECK`, `ENABLE_TESTING_MODES`.

### Nightscout layer

`tconnectsync/nightscout.py` wraps all Nightscout REST calls. `NS_IGNORE_CONN_ERRORS` and `NS_SKIP_TLS_VERIFY` modify its behavior. Parsers in `tconnectsync/parser/nightscout.py` convert domain events into NS treatment/entry JSON; prefer extending those parsers over constructing NS payloads inline in `sync/` processors.

## Testing notes

- `tests/conftest.py` forces `TIMEZONE_NAME=America/New_York` **before** any tconnectsync module is imported, and wipes cached modules so the env var actually sticks. If you add a test file that imports `tconnectsync` at module load, make sure conftest has run first (the standard pytest/unittest discovery already guarantees this).
- `tests/nightscout_fake.py` provides a fake Nightscout client used throughout the sync tests — use it rather than mocking HTTP calls by hand.
- `tests/api/fake.py` mirrors the `TConnectApi` surface for processor tests.
- The test suite is primarily in `tests/sync/tandemsource/` (the actively maintained v2 code path); legacy `tests/parser/`, `tests/api/`, and `tests/domain/` cover the supporting layers.

## Docker image & deploy

The fork publishes its own Docker image to `ghcr.io/xannasavin/tconnectsync` via `.github/workflows/publish-docker.yml`, triggered on push to `master`, on `v*` tags, and via `workflow_dispatch`. Production target is a container on the user's Synology (Portainer stack pulling `:latest`).

### Post-deploy: review Trivy findings

The publish workflow runs Trivy against the built image and **uploads findings to the GitHub Security tab as SARIF, but does NOT fail the build on HIGH/CRITICAL anymore** (intentional — see commit `ee02016`). This means CVEs do not gate deploys, so they MUST be reviewed manually after every successful publish run.

**After every push to master that produces a green Docker build:**

1. Open https://github.com/xannasavin/tconnectsync/security/code-scanning and filter by tool `Trivy` (or category `trivy-image`).
2. For each HIGH/CRITICAL finding, classify it:
   - **Patchable in this repo** (Python dep with a fix release available) → bump the version in `Pipfile`, regenerate `Pipfile.lock` with `pipenv lock`, commit.
   - **Patchable via base image** (Debian/OS package, fix in newer `python:3.12-slim-bookworm` build) → bump the Dockerfile base image pin or just rebuild — `python:3.12-slim-bookworm` is a moving tag and Debian fixes land regularly.
   - **Unpatchable / no fix yet** → dismiss with reason `Won't fix` and a one-line justification in the dismiss comment so the finding doesn't reappear noisily on the next scan.
3. Do not let the Security tab drift into "ignore everything" mode. The whole point of keeping the scan non-blocking was to avoid frustrating dev experience, NOT to make findings invisible — the manual review IS the gate.

If a critical CVE warrants stricter enforcement (e.g., an actively exploited zero-day in `requests` or `urllib3`), re-enable blocking by setting `exit-code: '1'` on the Trivy step in `publish-docker.yml` for that release window, then revert once patched.

## Project Planning

This project uses buzzwoo standard planning structure. See `.claude-bw/PLANNING.md` for folder conventions and usage guidelines.

**Quick reference:**
- Plans: `.claude/plans/{ID}-plan.md`
- Specs: `.claude-bw/specs/`
- PRD: `.claude-bw/prd/{ID-name}/`
- Questions: `.claude-bw/questions/`
- Context: `.claude-bw/context/`
