#
# Copyright (c) 2026, slng.ai
#
# SPDX-License-Identifier: BSD-2-Clause
#

"""Tests for WorldPart: regional host, URL building, and base_url migration."""

import pytest

from pipecat_slng import SlngSTTService, SlngTTSService, WorldPart


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
