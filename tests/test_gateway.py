#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Tests for WorldPart (regional host, URL building, base_url migration) and tracking headers."""

import pytest

from pipecat_slng import SlngSTTService, SlngTTSService, WorldPart
from pipecat_slng._gateway import tracking_headers


@pytest.mark.parametrize("world_part", list(WorldPart))
def test_every_world_part_has_regional_host(world_part):
    assert world_part.host == f"{world_part.value}.api.slng.ai"


def test_bridge_url_quotes_model_path():
    url = WorldPart.IN.bridge_url("tts", "slng/fish/tts:s2.1 pro")
    assert url == "wss://in.api.slng.ai/v1/bridges/unmute/tts/slng/fish/tts:s2.1%20pro"


def test_resolve_accepts_plain_string():
    assert WorldPart.resolve("us-west") is WorldPart.US_WEST


def test_resolve_rejects_unknown_world_part():
    with pytest.raises(ValueError, match="Use one of: eu-north, eu-west"):
        WorldPart.resolve("mars")


@pytest.mark.parametrize(
    "base_url", ["in.api.slng.ai", "https://in.api.slng.ai", "wss://in.api.slng.ai/"]
)
def test_matching_base_url_is_accepted_with_warning(base_url):
    with pytest.warns(DeprecationWarning, match="base_url is deprecated"):
        assert WorldPart.resolve("in", base_url) is WorldPart.IN


@pytest.mark.parametrize(
    "base_url", ["api.slng.ai", "https://api.slng.ai", "us-west.api.slng.ai"]
)
def test_base_url_not_matching_world_part_is_rejected(base_url):
    with pytest.raises(ValueError, match="does not match world_part 'in'"):
        WorldPart.resolve("in", base_url)


@pytest.mark.parametrize("service", [SlngSTTService, SlngTTSService])
def test_services_require_world_part(service):
    with pytest.raises(TypeError, match="world_part"):
        service(api_key="test-key")


@pytest.mark.parametrize("service", [SlngSTTService, SlngTTSService])
def test_services_reject_old_gateway(service):
    with pytest.raises(ValueError, match="no longer supported"):
        service(api_key="test-key", world_part="in", base_url="api.slng.ai")


def test_tracking_headers_strip_values():
    assert tracking_headers(" agent-1 ", "\tcall-9\n") == {
        "X-SLNG-Agent-Id": "agent-1",
        "X-SLNG-Session-Id": "call-9",
    }


def test_tracking_headers_none_sends_nothing():
    assert tracking_headers(None, None) == {}
    assert tracking_headers("agent-1", None) == {"X-SLNG-Agent-Id": "agent-1"}


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("", "must not be empty"),
        ("   ", "must not be empty"),
        ("a" * 129, "must be 128 characters or fewer"),
        ("a,b", "must not contain commas or control characters"),
        ("a\nb", "must not contain commas or control characters"),
        (123, "must be a string"),
    ],
)
@pytest.mark.parametrize("name", ["external_agent_id", "external_session_id"])
def test_tracking_headers_reject_bad_ids(name, value, message):
    with pytest.raises(ValueError, match=f"^{name} {message}$"):
        tracking_headers(
            **{"external_agent_id": None, "external_session_id": None, name: value}
        )


def test_tracking_id_of_128_chars_is_accepted():
    assert tracking_headers("a" * 128, None) == {"X-SLNG-Agent-Id": "a" * 128}


@pytest.mark.parametrize("service", [SlngSTTService, SlngTTSService])
def test_services_reject_bad_tracking_id(service):
    with pytest.raises(ValueError, match="external_session_id must not be empty"):
        service(api_key="test-key", world_part="in", external_session_id=" ")
