# SPDX-License-Identifier: AGPL-3.0-or-later
# Copyright (c) 2026 Yasuhiro / AIseed

"""The usage report never reaches the real endpoint: every client here is
an httpx.MockTransport."""

import json
from datetime import date

import httpx
import pytest

import aiseed_weather
from aiseed_weather.models import user_settings
from aiseed_weather.models.user_settings import UserSettings
from aiseed_weather.services import usage_report

_DAY1 = date(2026, 10, 18)
_DAY2 = date(2026, 10, 19)


@pytest.fixture
def usage_file(tmp_path, monkeypatch):
    path = tmp_path / "usage.json"
    monkeypatch.setattr(usage_report, "usage_path", lambda: path)
    return path


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _recorder(status=204):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(status)

    return seen, handler


@pytest.mark.asyncio
async def test_body_is_the_agreed_shape_without_ids(usage_file):
    seen, handler = _recorder()
    async with _client(handler) as client:
        sent = await usage_report.send_if_due(
            UserSettings(), client=client, today=_DAY1,
        )
    assert sent is True
    request = seen[0]
    assert str(request.url) == "https://analytics.aiseed.dev/v1/app"
    body = json.loads(request.content)
    assert set(body) == {"site", "version", "os", "channel"}
    assert body["site"] == "weather.time-j.net"
    assert body["version"] == aiseed_weather.__version__
    assert body["os"] in ("windows", "macos", "linux")
    assert body["channel"] in ("release", "source")
    # The honest name goes in the HTTP header only, not in the body.
    ua = request.headers["user-agent"]
    assert ua.startswith(f"AIseed Weather/{body['version']} (")
    assert "origin" not in request.headers
    assert json.loads(usage_file.read_text()) == {"last_sent": "2026-10-18"}


@pytest.mark.asyncio
async def test_release_build_is_told_apart_from_source(usage_file, monkeypatch):
    seen, handler = _recorder()
    monkeypatch.setenv(usage_report.BUILD_ENV, "release")
    async with _client(handler) as client:
        await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
    assert json.loads(seen[0].content)["channel"] == "release"


@pytest.mark.asyncio
async def test_switched_off_sends_nothing(usage_file):
    seen, handler = _recorder()
    async with _client(handler) as client:
        sent = await usage_report.send_if_due(
            UserSettings(usage_report=False), client=client, today=_DAY1,
        )
    assert sent is False
    assert seen == []
    assert not usage_file.exists()


@pytest.mark.asyncio
async def test_once_a_day_then_again_on_the_next_day(usage_file):
    seen, handler = _recorder()
    async with _client(handler) as client:
        assert await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
        assert not await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
        assert await usage_report.send_if_due(UserSettings(), client=client, today=_DAY2)
    assert len(seen) == 2


@pytest.mark.parametrize("status", [500, 403])
@pytest.mark.asyncio
async def test_refusal_is_not_recorded_and_is_retried(usage_file, status):
    seen, handler = _recorder(status)
    async with _client(handler) as client:
        assert not await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
        assert not await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
    assert len(seen) == 2
    assert not usage_file.exists()


@pytest.mark.parametrize(
    "error", [httpx.ConnectError("down"), httpx.ReadTimeout("slow")],
)
@pytest.mark.asyncio
async def test_network_failure_stays_silent_and_unrecorded(usage_file, error):
    def handler(request):
        raise error

    async with _client(handler) as client:
        sent = await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
    assert sent is False
    assert not usage_file.exists()


@pytest.mark.asyncio
async def test_broken_usage_file_is_sent_and_rewritten(usage_file):
    usage_file.write_text("{not json", encoding="utf-8")
    seen, handler = _recorder()
    async with _client(handler) as client:
        assert await usage_report.send_if_due(UserSettings(), client=client, today=_DAY1)
    assert len(seen) == 1
    assert json.loads(usage_file.read_text()) == {"last_sent": "2026-10-18"}


def test_missing_key_defaults_to_on():
    assert user_settings._from_mapping({}).usage_report is True


def test_false_turns_it_off():
    assert user_settings._from_mapping({"usage_report": False}).usage_report is False


@pytest.mark.parametrize("value", ["yes", "false", 1, 0])
def test_non_boolean_is_a_config_error(value):
    with pytest.raises(ValueError, match="usage_report"):
        user_settings._from_mapping({"usage_report": value})


def test_template_ships_it_on_and_parses():
    import tomllib

    assert tomllib.loads(user_settings.template())["usage_report"] is True


def test_version_is_known_in_a_source_checkout():
    assert aiseed_weather.__version__ != "0"


def test_missing_key_is_noticed_in_settings_but_a_written_key_is_not():
    assert user_settings._from_mapping({}).usage_report_written is False
    assert user_settings._from_mapping({"usage_report": True}).usage_report_written is True
    assert user_settings._from_mapping({"usage_report": False}).usage_report_written is True


@pytest.mark.asyncio
async def test_first_start_without_the_key_only_shows_the_notice(usage_file):
    settings = user_settings._from_mapping({})
    seen, handler = _recorder()
    assert usage_report.notice_due(settings)
    async with _client(handler) as client:
        assert await usage_report.start(settings, client=client, today=_DAY1) == "notice"
        assert seen == []
        assert json.loads(usage_file.read_text()) == {"notice_shown": "2026-10-18"}

        # The notice is recorded, so it is not shown again and sending begins.
        assert not usage_report.notice_due(settings)
        assert await usage_report.start(settings, client=client, today=_DAY2) == "sent"
    assert len(seen) == 1
    assert json.loads(usage_file.read_text()) == {
        "notice_shown": "2026-10-18", "last_sent": "2026-10-19",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("value", [True, False])
async def test_a_written_key_gets_no_notice(usage_file, value):
    settings = user_settings._from_mapping({"usage_report": value})
    seen, handler = _recorder()
    assert not usage_report.notice_due(settings)
    async with _client(handler) as client:
        result = await usage_report.start(settings, client=client, today=_DAY1)
    assert result == ("sent" if value else "skipped")
    assert len(seen) == (1 if value else 0)
    assert "notice_shown" not in (
        json.loads(usage_file.read_text()) if usage_file.exists() else {}
    )


def test_a_fresh_template_already_carries_the_key():
    import tomllib

    parsed = tomllib.loads(user_settings.template())
    assert user_settings._from_mapping(parsed).usage_report_written is True
