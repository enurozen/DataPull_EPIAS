# EPİAŞ Santral Data Puller

A Streamlit web app for pulling hourly realtime generation data for a power
plant ("santral") from the [EPİAŞ Transparency Platform](https://seffaflik.epias.com.tr/),
exporting the result as CSV.

It's a UI wrapper around the same two-stage flow as the original
`fetchKarapinarGES.py` script: log in to EPİAŞ to get a session ticket (TGT),
then use that ticket to pull hourly generation data for a plant over a date
range.

## Features

- Enter your EPİAŞ credentials, a plant ID, and a date range — no code editing required.
- Session ticket is cached per browser session and only refreshed when it
  actually expires, instead of logging in on every fetch.
- Multi-day ranges are fetched concurrently (5 requests in flight at a time)
  with automatic retry-with-backoff on transient network/server errors.
- Results shown in a sortable table with a one-click CSV download.
- Friendly error messages for invalid credentials, unknown plant IDs, and
  connectivity issues.

## Setup

Requires Python 3.9+.

```bash
pip install -r requirements.txt
```

## Run

```bash
streamlit run app.py
```

If `streamlit` isn't recognized as a command (common on Windows when pip's
scripts folder isn't on PATH), run it as a module instead:

```bash
python -m streamlit run app.py
```

This opens the app in your browser at `http://localhost:8501`.

## Usage

| Field | What to enter |
|---|---|
| EPİAŞ Username (Email) | Your EPİAŞ account email |
| API Token | Your EPİAŞ account **password** (masked). EPİAŞ doesn't issue a static API key — it exchanges your password for a short-lived session ticket on login, so this field takes the password that starts that exchange. |
| Santral Code | The numeric power plant ID (e.g. `2579` for Karapınar GES) |
| Start Date / End Date | The date range to fetch (inclusive). One API call is made per day in the range. |

Click **Fetch Data**. Results appear in a sortable table with a **Download as
CSV** button underneath.

## Security notes

- Credentials are entered at runtime in the browser and kept only in that
  browser session's server-side memory (Streamlit `session_state`) — they are
  never written to disk, logged, or committed to this repository.
- Do not hardcode credentials into `app.py` or any other file in this repo.
- If you deploy this somewhere other than your own machine, use a host that
  serves over HTTPS (e.g. Streamlit Community Cloud) so credentials aren't
  sent in the clear.

## Project structure

```
app.py             Streamlit app (API layer + UI layer)
requirements.txt   Pinned dependencies
tests/             Unit tests (pytest)
```

## Running tests

```bash
pip install -r requirements-dev.txt
pytest
```
