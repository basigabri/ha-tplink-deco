"""Tests for blocking and unblocking clients through the Deco blocklist."""

from __future__ import annotations

import base64
from datetime import datetime
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from unittest.mock import MagicMock

import aiohttp
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
import pytest
import pytest_asyncio
import voluptuous as vol

from custom_components.tplink_deco import async_setup
from custom_components.tplink_deco.api import TplinkDecoApi
from custom_components.tplink_deco.const import COORDINATOR_CLIENTS_KEY
from custom_components.tplink_deco.const import COORDINATOR_DECOS_KEY
from custom_components.tplink_deco.const import DOMAIN
from custom_components.tplink_deco.const import SERVICE_BLOCK_CLIENT
from custom_components.tplink_deco.const import SERVICE_UNBLOCK_CLIENT
from custom_components.tplink_deco.coordinator import TpLinkDecoClient
from custom_components.tplink_deco.exceptions import UnexpectedApiException
from custom_components.tplink_deco.services import deco_mac

MAC = "AA-BB-CC-DD-EE-FF"


# --- API ---------------------------------------------------------------------


@pytest.fixture
def api():
    session = MagicMock(spec=aiohttp.ClientSession)
    api = TplinkDecoApi(
        session=session,
        host="https://192.168.1.1",
        username="admin",
        password="password",
        verify_ssl=False,
    )
    api._stok = "token"
    api._cookie = "sysauth=abc"
    api._seq = 1
    api._encode_payload = lambda payload: payload
    api._async_post = AsyncMock(return_value={"error_code": 0})
    return api


def _posted(api):
    context, url = api._async_post.await_args.args
    kwargs = api._async_post.await_args.kwargs
    return context, url, kwargs["params"], kwargs["data"]


@pytest.mark.asyncio
async def test_block_sends_blocklist_entry(api):
    await api.async_block_client(MAC, "Kid's tablet")

    _, url, params, data = _posted(api)
    assert url == "https://192.168.1.1/cgi-bin/luci/;stok=token/admin/client"
    assert params == {"form": "black_list"}
    assert data == {
        "operation": "block",
        "params": {
            "client_list": [
                {
                    "mac": MAC,
                    "name": base64.b64encode(b"Kid's tablet").decode(),
                    "client_type": "other",
                }
            ]
        },
    }


@pytest.mark.asyncio
async def test_unblock_sends_bare_mac(api):
    await api.async_unblock_client(MAC)

    _, _, params, data = _posted(api)
    assert params == {"form": "black_list"}
    assert data == {"operation": "unblock", "params": {"client_list": [MAC]}}


@pytest.mark.asyncio
async def test_encrypted_reply_is_decrypted(api):
    api._async_post.return_value = {"data": "ciphertext"}
    api._decrypt_data = MagicMock(return_value={"error_code": 0})

    await api.async_unblock_client(MAC)

    api._decrypt_data.assert_called_once_with(f"Unblock Client {MAC}", "ciphertext")


@pytest.mark.asyncio
async def test_router_error_is_raised(api):
    api._async_post.return_value = {"error_code": -1}

    with pytest.raises(UnexpectedApiException, match="error_code=-1"):
        await api.async_block_client(MAC, "Phone")


# --- Client state ------------------------------------------------------------


@pytest.mark.parametrize(("raw", "expected"), [(True, True), (False, False)])
def test_client_reports_blocked_flag(raw, expected):
    client = TpLinkDecoClient(MAC)
    client.update(
        {"name": "Phone", "blocked": raw}, "DE-CO", datetime.now(timezone.utc)
    )
    assert client.blocked is expected


def test_client_without_blocked_field_is_not_blocked():
    client = TpLinkDecoClient(MAC)
    client.update({"name": "Phone"}, "DE-CO", datetime.now(timezone.utc))
    assert client.blocked is False


# --- Actions -----------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["aa:bb:cc:dd:ee:ff", "AA-BB-CC-DD-EE-FF", "aabb.ccdd.eeff", "AABBCCDDEEFF"],
)
def test_mac_is_normalised_to_dashes(value):
    assert deco_mac(value) == MAC


@pytest.mark.parametrize("value", ["", "AA:BB:CC", "GG:BB:CC:DD:EE:FF"])
def test_invalid_mac_is_rejected(value):
    with pytest.raises(vol.Invalid):
        deco_mac(value)


class FakeServices:
    def __init__(self):
        self.handlers = {}

    def async_register(self, domain, service, handler, schema, supports_response):
        self.handlers[(domain, service)] = (handler, schema)

    async def async_call(self, domain, service, data):
        handler, schema = self.handlers[(domain, service)]
        return await handler(SimpleNamespace(data=schema(data)))


class FakeConfigEntries:
    def __init__(self, entries):
        self.entries = {entry.entry_id: entry for entry in entries}

    def async_entries(self, domain):
        return [entry for entry in self.entries.values() if entry.domain == domain]

    def async_get_entry(self, entry_id):
        return self.entries.get(entry_id)


def _entry_data(clients):
    api = MagicMock()
    api.async_block_client = AsyncMock()
    api.async_unblock_client = AsyncMock()
    return {
        COORDINATOR_DECOS_KEY: MagicMock(api=api),
        COORDINATOR_CLIENTS_KEY: MagicMock(
            data=clients, async_request_refresh=AsyncMock()
        ),
    }


@pytest_asyncio.fixture
async def hass():
    phone = TpLinkDecoClient(MAC)
    phone.name = "Phone"
    entries = [
        SimpleNamespace(
            entry_id=entry_id,
            domain=DOMAIN,
            state=ConfigEntryState.LOADED,
            title=entry_id.title(),
        )
        for entry_id in ("first", "second")
    ]
    hass = SimpleNamespace(
        data={
            DOMAIN: {
                "first": _entry_data({MAC: phone}),
                "second": _entry_data({}),
            }
        },
        config_entries=FakeConfigEntries(entries),
        services=FakeServices(),
        tasks=[],
    )
    hass.async_create_task = hass.tasks.append
    assert await async_setup(hass, {}) is True
    return hass


async def _assert_refreshes_in_background(hass, data):
    """The action returns before the slow client refresh, which runs after."""
    refresh = data[COORDINATOR_CLIENTS_KEY].async_request_refresh
    refresh.assert_called_once()
    refresh.assert_not_awaited()
    assert len(hass.tasks) == 1
    await hass.tasks.pop()
    refresh.assert_awaited_once()


def _data(hass, entry_id):
    return hass.data[DOMAIN][entry_id]


@pytest.mark.asyncio
async def test_block_uses_known_client_name_and_refreshes(hass):
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BLOCK_CLIENT,
        {"config_entry_id": "first", "mac_address": "aa:bb:cc:dd:ee:ff"},
    )

    data = _data(hass, "first")
    data[COORDINATOR_DECOS_KEY].api.async_block_client.assert_awaited_once_with(
        MAC, "Phone"
    )
    await _assert_refreshes_in_background(hass, data)
    _data(hass, "second")[
        COORDINATOR_DECOS_KEY
    ].api.async_block_client.assert_not_awaited()


@pytest.mark.asyncio
async def test_block_unknown_client_falls_back_to_mac_name(hass):
    await hass.services.async_call(
        DOMAIN,
        SERVICE_BLOCK_CLIENT,
        {"config_entry_id": "second", "mac_address": "11:22:33:44:55:66"},
    )

    _data(hass, "second")[
        COORDINATOR_DECOS_KEY
    ].api.async_block_client.assert_awaited_once_with(
        "11-22-33-44-55-66", "11-22-33-44-55-66"
    )


@pytest.mark.asyncio
async def test_unblock_targets_entry_and_refreshes(hass):
    await hass.services.async_call(
        DOMAIN,
        SERVICE_UNBLOCK_CLIENT,
        {"config_entry_id": "first", "mac_address": MAC},
    )

    data = _data(hass, "first")
    data[COORDINATOR_DECOS_KEY].api.async_unblock_client.assert_awaited_once_with(MAC)
    await _assert_refreshes_in_background(hass, data)


@pytest.mark.asyncio
async def test_omitted_entry_is_ambiguous_when_two_are_loaded(hass):
    with pytest.raises(ServiceValidationError, match="config_entry_id"):
        await hass.services.async_call(
            DOMAIN, SERVICE_BLOCK_CLIENT, {"mac_address": MAC}
        )


@pytest.mark.asyncio
async def test_action_rejects_invalid_mac(hass):
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN,
            SERVICE_BLOCK_CLIENT,
            {"config_entry_id": "first", "mac_address": "not-a-mac"},
        )
