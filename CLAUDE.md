# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Purpose

`tconnectsync` is a one-way synchronizer: **Tandem Source → Nightscout**. It pulls basal, bolus, pump event, profile, and optional CGM data from Tandem's undocumented APIs and uploads it to a Nightscout instance as treatments/entries.

As of **v3.0.0** the only data source is the Tandem Source **BFF** API (`api/reports/bff/*`). Tandem retired the previous `api/reports/reportsfacade/*` endpoints without warning — US on 2026-06-30 (upstream issue #146), the EU region on 2026-07-16 around 23:00 local. Anything older than v3.0.0 returns HTTP 404 and cannot sync. The legacy t:connect API clients (`controliq`, `ws2`, `android`, `webui`) were deleted in the same release; there are no fallback paths left.

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

CI (`.github/workflows/python-package.yml`) runs on Python 3.12 and 3.13 and executes: `flake8` (syntax-errors-only gate, then full warnings pass), `tconnectsync --help` smoke test, `pytest`, and `coverage run -m unittest`. Matching that order locally is the fastest way to mirror CI.

**Local `unittest` runs need the CI timezone.** `secret.py` reads `~/.config/tconnectsync/.env` at import, so a personal `TIMEZONE_NAME` there overrides the fixtures — `conftest.py` guards against this, but it is a pytest mechanism and `unittest discover` never loads it. Use `TIMEZONE_NAME=America/New_York python -m unittest discover`; plain `pytest` needs no override. Failures without it are not regressions.

## Architecture

### Entry flow

`main.py` → `tconnectsync.main()` in `tconnectsync/__init__.py`. That function:

1. Parses CLI args and configures logging.
2. Builds a `TConnectApi` (email/password/region) and a `NightscoutApi` (URL/secret).
3. Either runs `check_login`, enters the `TandemSourceAutoupdate` loop (with `--auto-update`), or runs a single `TandemSourceProcessTimeRange(...).process(time_start, time_end)` and exits.

Auto-update polls `tandemsource.pump_events` via `ChooseDevice` → `ProcessTimeRange`, adjusting sleep intervals based on `AUTOUPDATE_*` env vars in `secret.py`. Each poll covers the trailing 24 hours, so a short outage backfills itself once syncing resumes; only gaps older than a day need a manual `--start-date` run.

**Failure handling in the loop** (see `sync/tandemsource/autoupdate.py`): transient network errors and API errors that `get()` does not retry itself (it only handles 401 and 500) are caught inside the loop and retried with exponential backoff — 30s doubling, capped at `AUTOUPDATE_DEFAULT_SLEEP_SECONDS`, reset on any successful poll. Do not "simplify" this into letting exceptions propagate: exiting discards the credentials cache, so a container restart loop becomes a login storm against `sso.tandemdiabetes.com` and risks a WAF ban. After `AUTOUPDATE_API_FAILURE_MINUTES` of unbroken failure the loop deliberately exits non-zero so the platform can alert; `ApiLoginException` stays fatal because bad credentials are not transient.

### API layer (`tconnectsync/api/`)

`TConnectApi` is a lazy wrapper around a single client, `tandemsource.py`. The legacy clients were deleted in v3.0.0 — do not reintroduce imports of `controliq`, `ws2`, `android`, or `webui`.

Key points when touching this layer:

- The BFF endpoints are `api/reports/bff/pumper/{pumperId}` (device list) and `api/reports/bff/pump-logs/{deviceId}` (events). The device key is `assignmentId`, and dates are `maxDateOfEvents` / `availableDataRange.start`.
- BFF date fields are **naive pump-local wall-clock strings**. Route them through `naive_local_to_utc()` before comparing against `arrow.utcnow()` / `time.time()`, or timestamps land in the future and poison the autoupdate cadence.
- The WAF enforces same-origin: `Origin`/`Referer` must match `SOURCE_URL`, or requests get HTTP 403.
- Mind the `needs_relogin()` / lazy-instantiation pattern — hitting a stale token triggers a full re-login.
- `region` selects between `_US_URLS` and `_EU_URLS`; the two regions have different OIDC client ids and cut over to new APIs on different dates.

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

Two flags survived v3.0.0 as names but lost their backing:

- **`IOB`** is dead. It was served by `ws2.py`, which was deleted; no processor implements it, so selecting it does nothing. Don't assume it works because the flag accepts it.
- **`DEVICE_STATUS`** depends on event 81, which the BFF API no longer reliably returns. It degrades gracefully rather than failing.

### Event parser (`tconnectsync/eventparser/`)

`events.py` is **autogenerated** from `events.json` by `build_events.py`. The generated file has a `# THIS FILE IS AUTOGENERATED. DO NOT EDIT.` header — edits will be overwritten.

- To update for new Tandem firmware: run `scripts/sync_tandem_events.py <URL-to-reports-module-chunk.js>` to download the latest minified bundle, extract the `JSON.parse(...)` payload, update `events.json`, and regenerate `events.py`.
- `custom_events.json` holds overrides layered on top of the upstream dump.
- **Since v3.0.0 the live path is JSON, not binary.** The BFF `pump-logs` endpoint returns events the server has already decoded, and `Event()` / `Events()` build objects straight from that JSON. The `RawEvent` / `EVENT_LEN = 26` binary struct decoding still exists and is still used by tests and fixtures, but Tandem no longer sends the raw blob — don't reach for it when adding a new event type.

### Configuration (`tconnectsync/secret.py`)

All config is loaded via `python-dotenv` from (in order): `./.env`, `~/.config/tconnectsync/.env`, or process env vars. `secret.py` is imported at package load — any new config value must be added there and read via the `get` / `get_bool` / `get_number` / `get_one_of` helpers so it participates in the same precedence. Do not read `os.environ` directly elsewhere in the codebase.

Notable flags consumed across the codebase: `TCONNECT_REGION` (`US`/`EU`), `PUMP_SERIAL_NUMBER` (optional — when unset, most-recently-used pump is chosen), `AUTOUPDATE_*` sleep/failure tuning (all nine are documented in the README's "Tuning Auto-Update" table — keep it in sync when adding one), `NIGHTSCOUT_PROFILE_UPLOAD_MODE` (`add`/`replace`), `FETCH_ALL_EVENT_TYPES` (also auto-enabled when `DEVICE_STATUS` feature is on; note the BFF server currently ignores the `eventIds` filter and returns everything regardless), `IGNORE_ZERO_UNIT_BASAL`, `SKIP_NS_LAST_UPLOADED_CHECK`, `ENABLE_TESTING_MODES`.

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
   - **Patchable in this repo** (Python dep with a fix release available) → bump it in **`setup.cfg`** under `install_requires`, then regenerate `Pipfile.lock` with `pipenv lock` and commit both. Note the `Pipfile` declares only `tconnectsync = {path = "."}` — runtime dependencies are *not* listed there, so editing the Pipfile does nothing.
   - **No longer used at all** → delete it from `install_requires` rather than bumping. v3.0.0 deleted the HTML scrapers, so check whether anything still imports the package before assuming a bump is needed.
   - **Patchable via base image** (Debian/OS package, fix in newer `python:3.12-slim-bookworm` build) → bump the Dockerfile base image pin or just rebuild — `python:3.12-slim-bookworm` is a moving tag and Debian fixes land regularly.
   - **Unpatchable / no fix yet** → dismiss with reason `Won't fix` and a one-line justification in the dismiss comment so the finding doesn't reappear noisily on the next scan.
3. Do not let the Security tab drift into "ignore everything" mode. The whole point of keeping the scan non-blocking was to avoid frustrating dev experience, NOT to make findings invisible — the manual review IS the gate.

If a critical CVE warrants stricter enforcement (e.g., an actively exploited zero-day in `requests` or `urllib3`), re-enable blocking by setting `exit-code: '1'` on the Trivy step in `publish-docker.yml` for that release window, then revert once patched.

### Post-deploy: always hand the user the exact image tag

**After every green Docker build, tell the user the exact image line to paste into the Portainer stack — unprompted.** Never say "pull the latest image": Portainer's "Re-pull image and redeploy" reports success while sending no pull to the Docker daemon ([portainer#13173](https://github.com/portainer/portainer/issues/13173), broken since 2.39.2 LTS), so `:latest` and `:master` silently keep the old build. An immutable per-commit tag cannot be faked from cache — Docker has to fetch it.

Do **not** derive the tag from `git rev-parse HEAD`. Commits marked `[skip ci]` produce no image, so master's tip and the newest image routinely differ (on 2026-07-17, master was at `5cd0462` while the image was `master-8523a6c`). Ask the registry which build actually exists:

```bash
gh api "repos/xannasavin/tconnectsync/actions/workflows/publish-docker.yml/runs?status=success&per_page=20" \
  -q '.workflow_runs | sort_by(.created_at) | reverse | .[0]
      | "ghcr.io/xannasavin/tconnectsync:master-\(.head_sha[0:7])   (built \(.created_at))"'
```

Use `gh api` with explicit sorting, not `gh run list --limit 1` — the latter has been observed returning a stale run.

Then give the user the line verbatim, plus what the container must log to prove the deploy landed:

```yaml
image: ghcr.io/xannasavin/tconnectsync:master-<sha>
```

```
tconnectsync <version> (revision <sha>..., built <date>)
```

Container logs are in **UTC**; do not compare them against local timestamps when judging whether a restart predates a build.

## Project Planning

This project uses buzzwoo standard planning structure. See `.claude-bw/PLANNING.md` for folder conventions and usage guidelines.

**Quick reference:**
- Plans: `.claude/plans/{ID}-plan.md`
- Specs: `.claude-bw/specs/`
- PRD: `.claude-bw/prd/{ID-name}/`
- Questions: `.claude-bw/questions/`
- Context: `.claude-bw/context/`
