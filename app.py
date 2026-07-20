"""
Streamlit web app for pulling power plant ("santral") generation data from the
EPİAŞ Transparency Platform.

This is a UI wrapper around the two-stage flow used by the original
fetchKarapinarGES.py script:
  1. Log in to EPİAŞ (username + password) to obtain a TGT session ticket.
  2. Use that ticket to call the realtime-generation-bulk endpoint for a
     given power plant ID and date, and build a table of hourly generation.

Run locally with:
    streamlit run app.py
"""

import hashlib
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta
from typing import Any, Callable, Optional

import pandas as pd
import requests
import streamlit as st

CAS_URL = "https://giris.epias.com.tr/cas/v1/tickets"
GENERATION_URL = (
    "https://seffaflik.epias.com.tr/electricity-service/v1/generation/data/"
    "realtime-generation-bulk"
)

REQUEST_TIMEOUT_LOGIN = 10
REQUEST_TIMEOUT_DATA = 15

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_WORKERS = 5


class EpiasError(Exception):
    """A user-facing error while talking to the EPİAŞ API."""


class TokenExpiredError(EpiasError):
    """Raised when the API rejects the current TGT; caller should re-authenticate."""


# --------------------------------------------------------------------------
# API layer
# --------------------------------------------------------------------------

def _post_with_retries(url: str, *, timeout: float, error_context: str, **kwargs: Any) -> requests.Response:
    """POST with exponential-backoff retries for transient failures.

    Retries on connection errors, timeouts, and 5xx responses (the server's
    problem, likely to pass on retry). Client errors like 401/403/404 are
    returned as-is on the first try, since retrying won't change them.
    """
    last_exc: Optional[Exception] = None

    for attempt in range(MAX_RETRIES + 1):
        try:
            response = requests.post(url, timeout=timeout, **kwargs)
        except requests.exceptions.RequestException as exc:
            last_exc = exc
        else:
            if response.status_code < 500:
                return response
            last_exc = EpiasError(
                f"{error_context}: EPİAŞ server error (HTTP {response.status_code})."
            )

        if attempt < MAX_RETRIES:
            time.sleep(BACKOFF_BASE_SECONDS * (2**attempt))

    if isinstance(last_exc, requests.exceptions.RequestException):
        raise EpiasError(f"{error_context}: {last_exc}") from last_exc
    raise last_exc  # the EpiasError built above for repeated 5xx responses


def get_tgt(username: str, password: str) -> str:
    """Authenticate with EPİAŞ and return a TGT session ticket.

    The "API Token" field in the UI is the EPİAŞ account password: EPİAŞ
    does not issue a static API key, it issues a short-lived ticket (TGT)
    in exchange for username/password on every login.
    """
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "Accept": "text/plain",
    }
    payload = {"username": username, "password": password}

    response = _post_with_retries(
        CAS_URL,
        timeout=REQUEST_TIMEOUT_LOGIN,
        error_context="Could not reach the EPİAŞ login server",
        headers=headers,
        data=payload,
    )

    if response.status_code == 201:
        return response.text.strip()
    if response.status_code == 401:
        raise EpiasError(
            "Invalid Token: EPİAŞ rejected the supplied username/password."
        )
    raise EpiasError(f"EPİAŞ login failed (HTTP {response.status_code}).")


def _credentials_fingerprint(username: str, password: str) -> str:
    """Hash credentials so we can detect whether cached login state is still valid,
    without keeping a second plaintext copy of the password around."""
    return hashlib.sha256(f"{username}:{password}".encode("utf-8")).hexdigest()


def get_cached_tgt(username: str, password: str, force_refresh: bool = False) -> str:
    """Return a TGT for these credentials, reusing one already in session_state.

    A fresh TGT is only requested when there is none cached yet, the
    username/password changed since the cached one was issued, or
    force_refresh is set (used after the API reports the cached ticket expired).
    """
    fingerprint = _credentials_fingerprint(username, password)
    cached_fingerprint = st.session_state.get("tgt_fingerprint")

    if not force_refresh and st.session_state.get("tgt") and cached_fingerprint == fingerprint:
        return st.session_state["tgt"]

    tgt = get_tgt(username, password)
    st.session_state["tgt"] = tgt
    st.session_state["tgt_fingerprint"] = fingerprint
    return tgt


def fetch_generation_for_date(tgt: str, plant_id: int, day: date) -> list[dict[str, Any]]:
    """Fetch one day of hourly generation data for a single power plant."""
    headers = {
        "TGT": tgt,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    payload = {
        "date": f"{day.isoformat()}T00:00:00+03:00",
        "powerPlantIds": [plant_id],
    }

    response = _post_with_retries(
        GENERATION_URL,
        timeout=REQUEST_TIMEOUT_DATA,
        error_context=f"Connection error while fetching data for {day}",
        json=payload,
        headers=headers,
    )

    if response.status_code in (401, 403):
        raise TokenExpiredError(
            "Invalid Token: your session token was rejected or has expired."
        )
    if response.status_code == 404:
        raise EpiasError(f"Santral Code not found: no power plant with ID {plant_id}.")
    if not response.ok:
        raise EpiasError(
            f"EPİAŞ API returned an error (HTTP {response.status_code}) for {day}."
        )

    body = response.json()
    rows = body.get("items", body) if isinstance(body, dict) else body
    return rows or []


def fetch_generation_range(
    tgt: str,
    plant_id: int,
    start: date,
    end: date,
    progress_callback: Optional[Callable[[float], None]] = None,
) -> pd.DataFrame:
    """Fetch generation data for every day in [start, end] and combine into a DataFrame.

    Days are fetched concurrently (capped at MAX_WORKERS) since each day is an
    independent request; a fixed worker cap keeps this from hammering the API
    on wide date ranges.
    """
    total_days = (end - start).days + 1
    days = [start + timedelta(days=i) for i in range(total_days)]
    all_rows: list[dict[str, Any]] = []
    completed = 0

    with ThreadPoolExecutor(max_workers=min(MAX_WORKERS, total_days)) as executor:
        future_to_day = {
            executor.submit(fetch_generation_for_date, tgt, plant_id, day): day
            for day in days
        }
        try:
            for future in as_completed(future_to_day):
                rows = future.result()  # re-raises any error from that day's request
                for row in rows:
                    raw_date = row.get("date", "")
                    all_rows.append(
                        {
                            "Date": raw_date.split("T")[0] if "T" in raw_date else raw_date,
                            "Hour": row.get("hour", "00:00"),
                            "Generation (MWh)": row.get("sun", row.get("total", 0)),
                        }
                    )
                completed += 1
                if progress_callback:
                    progress_callback(completed / total_days)
        finally:
            # Best-effort: stop any not-yet-started requests once one day fails.
            for pending_future in future_to_day:
                pending_future.cancel()

    df = pd.DataFrame(all_rows)
    if not df.empty:
        df = df.sort_values(["Date", "Hour"]).reset_index(drop=True)
    return df


# --------------------------------------------------------------------------
# UI layer
# --------------------------------------------------------------------------

def render_sidebar() -> dict[str, Any]:
    """Render the input form and return the current field values."""
    st.header("Connection & Query")

    username = st.text_input(
        "EPİAŞ Username (Email)", placeholder="you@example.com"
    )
    api_token = st.text_input(
        "API Token",
        type="password",
        help=(
            "This is your EPİAŞ account password. EPİAŞ has no static API key; "
            "it exchanges your password for a short-lived session ticket on each login."
        ),
    )
    santral_code = st.text_input("Santral Code", placeholder="e.g. 2579")

    col1, col2 = st.columns(2)
    with col1:
        start_date = st.date_input("Start Date", value=date.today() - timedelta(days=1))
    with col2:
        end_date = st.date_input("End Date", value=date.today() - timedelta(days=1))

    st.caption("One API call is made per day in the selected range.")
    fetch_clicked = st.button("Fetch Data", type="primary", use_container_width=True)

    return {
        "username": username,
        "api_token": api_token,
        "santral_code": santral_code,
        "start_date": start_date,
        "end_date": end_date,
        "fetch_clicked": fetch_clicked,
    }


def validate_inputs(inputs: dict[str, Any]) -> Optional[str]:
    """Return an error message if inputs are invalid, otherwise None."""
    if not inputs["username"] or not inputs["api_token"]:
        return "Please fill in both the username and API Token fields."
    if not inputs["santral_code"].strip().isdigit():
        return "Santral Code must be numeric (e.g. 2579)."
    if inputs["start_date"] > inputs["end_date"]:
        return "Start Date cannot be after End Date."
    return None


def run_fetch(inputs: dict[str, Any]) -> None:
    """Execute the fetch flow and store the result in session state."""
    plant_id = int(inputs["santral_code"].strip())

    def progress_callback(progress_bar):
        return lambda p: progress_bar.progress(
            p, text=f"Fetching generation data... {int(p * 100)}%"
        )

    try:
        with st.spinner("Authenticating with EPİAŞ..."):
            tgt = get_cached_tgt(inputs["username"], inputs["api_token"])

        progress_bar = st.progress(0.0, text="Fetching generation data...")
        try:
            df = fetch_generation_range(
                tgt,
                plant_id,
                inputs["start_date"],
                inputs["end_date"],
                progress_callback=progress_callback(progress_bar),
            )
        except TokenExpiredError:
            # Cached ticket was stale (e.g. expired between clicks) - get a
            # fresh one and retry the fetch exactly once.
            with st.spinner("Session expired, re-authenticating..."):
                tgt = get_cached_tgt(
                    inputs["username"], inputs["api_token"], force_refresh=True
                )
            df = fetch_generation_range(
                tgt,
                plant_id,
                inputs["start_date"],
                inputs["end_date"],
                progress_callback=progress_callback(progress_bar),
            )
        progress_bar.empty()

        if df.empty:
            st.warning(
                "No data was returned for the selected range. "
                "The plant may not have generated power, or the Santral Code may be wrong."
            )
            st.session_state.result_df = None
        else:
            st.session_state.result_df = df
            st.session_state.result_meta = {
                "plant_id": plant_id,
                "start_date": inputs["start_date"],
                "end_date": inputs["end_date"],
            }
            st.success(f"Fetched {len(df)} rows.")
    except EpiasError as exc:
        st.error(str(exc))
        st.session_state.result_df = None


def render_results() -> None:
    """Render the results table and CSV download button, if data is available."""
    df = st.session_state.get("result_df")
    if df is None:
        return

    st.subheader("Generation Data")
    st.dataframe(df, use_container_width=True, hide_index=True)

    meta = st.session_state.get("result_meta", {})
    file_name = (
        f"santral_{meta.get('plant_id', 'data')}_"
        f"{meta.get('start_date', '')}_{meta.get('end_date', '')}.csv"
    )
    csv_bytes = df.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        label="Download as CSV",
        data=csv_bytes,
        file_name=file_name,
        mime="text/csv",
    )


def main() -> None:
    st.set_page_config(page_title="EPİAŞ Santral Data Puller", page_icon="⚡", layout="wide")
    st.title("⚡ EPİAŞ Santral Data Puller")
    st.caption(
        "Fetch hourly realtime generation data for an EPİAŞ power plant "
        "and export it to CSV."
    )

    if "result_df" not in st.session_state:
        st.session_state.result_df = None

    with st.sidebar:
        inputs = render_sidebar()

    if inputs["fetch_clicked"]:
        error = validate_inputs(inputs)
        if error:
            st.error(error)
        else:
            run_fetch(inputs)

    render_results()


if __name__ == "__main__":
    main()
