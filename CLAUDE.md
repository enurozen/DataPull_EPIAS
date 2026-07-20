# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A single-file Streamlit app (`app.py`) that pulls hourly realtime generation data for an EPİAŞ power plant ("santral") from the EPİAŞ Transparency Platform and exports it as CSV. It's a UI wrapper around the original `fetchKarapinarGES.py` script's two-stage flow: authenticate to get a session ticket, then use that ticket to fetch generation data.

## Commands

Install runtime deps:
```bash
pip install -r requirements.txt
```

Install dev deps (note: the file is `requirementsdev.txt`, no hyphen — despite `README.md` and `.github/workflows/ci.yml` both referring to it as `requirements-dev.txt`):
```bash
pip install -r requirementsdev.txt
```

Run the app:
```bash
streamlit run app.py
# or, if `streamlit` isn't on PATH (common on Windows):
python -m streamlit run app.py
```
Opens at `http://localhost:8501`.

Run tests:
```bash
pytest
```
Run a single test:
```bash
pytest tests/test_app.py::test_get_tgt_success
```

Lint (used in CI):
```bash
ruff check .
```

CI (`.github/workflows/ci.yml`) runs `ruff check .` then `pytest` on every push/PR to `main`.

## Architecture

`app.py` is split into two layers, marked by comment banners:

- **API layer** — talks to EPİAŞ over HTTP:
  - `get_tgt(username, password)` — POSTs to the CAS endpoint (`CAS_URL`) to exchange EPİAŞ account credentials for a short-lived TGT (session ticket). EPİAŞ has no static API key; the "API Token" field in the UI is actually the account password.
  - `get_cached_tgt(...)` — wraps `get_tgt`, caching the TGT in `st.session_state` keyed by a SHA-256 fingerprint of the credentials, so a fresh login only happens when credentials change or `force_refresh=True` is passed.
  - `fetch_generation_for_date(tgt, plant_id, day)` — POSTs to the realtime-generation-bulk endpoint (`GENERATION_URL`) for one plant/day.
  - `fetch_generation_range(...)` — fans `fetch_generation_for_date` out across a date range using a `ThreadPoolExecutor` (capped at `MAX_WORKERS = 5`), then merges results into a sorted `pandas.DataFrame`.
  - `_post_with_retries(...)` — shared retry helper used by both HTTP calls: exponential backoff on connection errors, timeouts, and 5xx responses; 4xx responses are returned immediately without retrying since they won't change.
  - Errors are raised as `EpiasError` (generic) or `TokenExpiredError` (a subclass raised on 401/403 from the data endpoint, signaling the caller should refresh the TGT and retry).

- **UI layer** — Streamlit rendering and orchestration:
  - `render_sidebar()` — collects username/password/plant ID/date range inputs.
  - `validate_inputs(...)` — client-side validation before any network call.
  - `run_fetch(...)` — orchestrates the fetch: gets a cached TGT, calls `fetch_generation_range`, and on `TokenExpiredError` transparently refreshes the TGT and retries the whole range fetch exactly once.
  - `render_results()` — renders the results table and a CSV download button.
  - `main()` — wires the above together and holds `st.session_state.result_df` as the source of truth for what's displayed.

Tests (`tests/test_app.py`) mock `app.requests.post` and `app.st.session_state` directly rather than driving the Streamlit UI — they exercise the API layer functions and `validate_inputs` as plain Python.

## Security notes

- Credentials live only in server-side `st.session_state` for the browser session; never write them to disk, logs, or commit them.
- Don't hardcode credentials anywhere in the repo.
