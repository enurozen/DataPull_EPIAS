import sys
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app


def _mock_response(status_code, text="", json_data=None):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    resp.ok = status_code < 400
    if json_data is not None:
        resp.json.return_value = json_data
    return resp


# --------------------------------------------------------------------------
# get_tgt
# --------------------------------------------------------------------------

def test_get_tgt_success():
    resp = _mock_response(201, text="  TGT-abc123  \n")
    with patch("app.requests.post", return_value=resp):
        assert app.get_tgt("user@example.com", "secret") == "TGT-abc123"


def test_get_tgt_invalid_credentials():
    resp = _mock_response(401)
    with patch("app.requests.post", return_value=resp):
        with pytest.raises(app.EpiasError, match="Invalid Token"):
            app.get_tgt("user@example.com", "wrong")


def test_get_tgt_retries_on_5xx_then_succeeds():
    responses = [_mock_response(500), _mock_response(500), _mock_response(201, text="TGT-ok")]
    with patch("app.requests.post", side_effect=responses), patch("app.time.sleep"):
        assert app.get_tgt("user", "pass") == "TGT-ok"


def test_get_tgt_connection_error_does_not_retry_forever():
    with patch(
        "app.requests.post", side_effect=app.requests.exceptions.ConnectionError("no route")
    ), patch("app.time.sleep"):
        with pytest.raises(app.EpiasError, match="Could not reach"):
            app.get_tgt("user", "pass")


# --------------------------------------------------------------------------
# fetch_generation_for_date
# --------------------------------------------------------------------------

def test_fetch_generation_for_date_items_key():
    body = {"items": [{"date": "2026-07-01T00:00:00", "hour": "01:00", "sun": 5}]}
    resp = _mock_response(200, json_data=body)
    with patch("app.requests.post", return_value=resp):
        rows = app.fetch_generation_for_date("tgt", 2579, date(2026, 7, 1))
    assert rows == body["items"]


def test_fetch_generation_for_date_bare_list():
    body = [{"date": "2026-07-01T00:00:00", "hour": "02:00", "total": 7}]
    resp = _mock_response(200, json_data=body)
    with patch("app.requests.post", return_value=resp):
        rows = app.fetch_generation_for_date("tgt", 2579, date(2026, 7, 1))
    assert rows == body


def test_fetch_generation_for_date_token_expired():
    resp = _mock_response(401)
    with patch("app.requests.post", return_value=resp):
        with pytest.raises(app.TokenExpiredError):
            app.fetch_generation_for_date("tgt", 2579, date(2026, 7, 1))


def test_fetch_generation_for_date_plant_not_found():
    resp = _mock_response(404)
    with patch("app.requests.post", return_value=resp):
        with pytest.raises(app.EpiasError, match="Santral Code not found"):
            app.fetch_generation_for_date("tgt", 999999, date(2026, 7, 1))


# --------------------------------------------------------------------------
# fetch_generation_range
# --------------------------------------------------------------------------

def test_fetch_generation_range_aggregates_and_sorts():
    def fake_fetch(tgt, plant_id, day):
        return [{"date": f"{day.isoformat()}T00:00:00", "hour": "00:00", "sun": 10}]

    with patch("app.fetch_generation_for_date", side_effect=fake_fetch):
        df = app.fetch_generation_range("tgt", 2579, date(2026, 7, 1), date(2026, 7, 3))

    assert list(df["Date"]) == ["2026-07-01", "2026-07-02", "2026-07-03"]
    assert len(df) == 3


def test_fetch_generation_range_propagates_errors():
    def fake_fetch(tgt, plant_id, day):
        if day == date(2026, 7, 2):
            raise app.EpiasError("boom")
        return []

    with patch("app.fetch_generation_for_date", side_effect=fake_fetch):
        with pytest.raises(app.EpiasError, match="boom"):
            app.fetch_generation_range("tgt", 2579, date(2026, 7, 1), date(2026, 7, 3))


def test_fetch_generation_range_reports_progress():
    progress_values = []

    with patch("app.fetch_generation_for_date", return_value=[]):
        app.fetch_generation_range(
            "tgt",
            2579,
            date(2026, 7, 1),
            date(2026, 7, 5),
            progress_callback=progress_values.append,
        )

    assert len(progress_values) == 5
    assert progress_values[-1] == 1.0


# --------------------------------------------------------------------------
# get_cached_tgt
# --------------------------------------------------------------------------

def test_get_cached_tgt_reuses_ticket(monkeypatch):
    monkeypatch.setattr(app.st, "session_state", {})

    with patch("app.get_tgt", side_effect=["TGT-1", "TGT-2"]) as mock_get_tgt:
        first = app.get_cached_tgt("user", "pass")
        second = app.get_cached_tgt("user", "pass")

    assert first == second == "TGT-1"
    assert mock_get_tgt.call_count == 1


def test_get_cached_tgt_refreshes_on_credential_change(monkeypatch):
    monkeypatch.setattr(app.st, "session_state", {})

    with patch("app.get_tgt", side_effect=["TGT-1", "TGT-2"]) as mock_get_tgt:
        first = app.get_cached_tgt("user", "pass1")
        second = app.get_cached_tgt("user", "pass2")

    assert (first, second) == ("TGT-1", "TGT-2")
    assert mock_get_tgt.call_count == 2


def test_get_cached_tgt_force_refresh(monkeypatch):
    monkeypatch.setattr(app.st, "session_state", {})

    with patch("app.get_tgt", side_effect=["TGT-1", "TGT-2"]) as mock_get_tgt:
        first = app.get_cached_tgt("user", "pass")
        second = app.get_cached_tgt("user", "pass", force_refresh=True)

    assert (first, second) == ("TGT-1", "TGT-2")
    assert mock_get_tgt.call_count == 2


# --------------------------------------------------------------------------
# validate_inputs
# --------------------------------------------------------------------------

def _inputs(**overrides):
    base = {
        "username": "user@example.com",
        "api_token": "secret",
        "santral_code": "2579",
        "start_date": date(2026, 7, 1),
        "end_date": date(2026, 7, 2),
        "fetch_clicked": True,
    }
    base.update(overrides)
    return base


def test_validate_inputs_ok():
    assert app.validate_inputs(_inputs()) is None


def test_validate_inputs_missing_credentials():
    assert "username and API Token" in app.validate_inputs(_inputs(username=""))


def test_validate_inputs_non_numeric_santral_code():
    assert "numeric" in app.validate_inputs(_inputs(santral_code="abc"))


def test_validate_inputs_bad_date_range():
    msg = app.validate_inputs(
        _inputs(start_date=date(2026, 7, 5), end_date=date(2026, 7, 1))
    )
    assert "Start Date" in msg
