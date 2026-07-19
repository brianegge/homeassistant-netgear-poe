"""Tests for firmware install on the JSON CGI backend (GS310TP-class).

The install path (file_http_download -> file_dualStatus -> file_dualConf ->
sys_reboot) was reverse-engineered from a packet capture and is NOT verified
against a live flash, so these cover the wire fields, the progress ordering and
— most importantly — the fail-closed guards that stop before anything
irreversible if the image did not land where expected.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from custom_components.netgear_poe.api import (
    DualImageStatus,
    NetgearError,
    NetgearPoeApi,
    SwitchInfo,
    _parse_dual_status,
    _resolve_slot,
)
from custom_components.netgear_poe.api_json_v2 import NetgearJsonV2Api

from .conftest import parse_upload_payload


def _dis(img1: str, img2: str, active: str, nxt: str | None = None) -> DualImageStatus:
    """A parsed DualImageStatus with the two slot versions."""
    return DualImageStatus(
        versions={"image1": img1, "image2": img2},
        current_active=active,
        next_active=nxt or active,
    )


def _dual_status(img1: str, img2: str, cur: object, nxt: object) -> dict:
    """A file_dualStatus response (one-row status list)."""
    return {
        "data": {
            "status": [
                {"img1Ver": img1, "img2Ver": img2, "curAct": cur, "nextAct": nxt}
            ]
        }
    }


def test_both_backends_support_install() -> None:
    """Base and the aj4 subclass both advertise firmware install."""
    assert NetgearPoeApi.supports_firmware_install is True
    assert NetgearJsonV2Api.supports_firmware_install is True


# --- file_dualStatus parsing ------------------------------------------------


def test_parse_dual_status_slot_numbers() -> None:
    """curAct/nextAct given as slot numbers map to image1/image2 keys."""
    status = _parse_dual_status(_dual_status("1.0.1.2", "1.0.0.9", "1", "1"))
    assert status.versions == {"image1": "1.0.1.2", "image2": "1.0.0.9"}
    assert status.current_active == "image1"
    assert status.next_active == "image1"
    assert status.inactive == "image2"


def test_parse_dual_status_slot_names() -> None:
    """The "image2"/"img2" spellings are tolerated too."""
    status = _parse_dual_status(_dual_status("1.0.1.2", "1.0.5.12", "image2", "img2"))
    assert status.current_active == "image2"
    assert status.next_active == "image2"
    assert status.inactive == "image1"


def test_resolve_slot_by_version_string() -> None:
    """A version-string marker resolves to the slot holding that version."""
    versions = {"image1": "1.0.1.2", "image2": "1.0.5.12"}
    assert _resolve_slot("1.0.5.12", versions) == "image2"
    assert _resolve_slot("1.0.1.2", versions) == "image1"


async def test_get_image_status_reads_file_dualstatus() -> None:
    """async_get_image_status fetches file_dualStatus and parses it."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(
        return_value=_dual_status("1.0.1.2", "1.0.0.9", "1", "2")
    )
    status = await api.async_get_image_status()
    api._authed_request.assert_awaited_once_with("get.cgi", "file_dualStatus")
    assert status.current_active == "image1"
    assert status.next_active == "image2"


# --- upload -----------------------------------------------------------------


def _upload_session() -> MagicMock:
    """A session whose post() yields a benign response context manager."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.read = AsyncMock(return_value=b"")
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=resp)
    ctx.__aexit__ = AsyncMock(return_value=False)
    session = MagicMock()
    session.post = MagicMock(return_value=ctx)
    session.close = AsyncMock()
    return session


async def test_upload_posts_to_httpupload_cgi_then_polls() -> None:
    """Upload posts the image to the bare httpupload.cgi with the slot in imgName."""
    session = _upload_session()
    api = NetgearPoeApi("host", "pw", session=session)
    api._xsid_header = "XSIDTOKEN"
    # The flash-write poll comes back done immediately.
    api._authed_request = AsyncMock(return_value={"data": {"status": "success"}})

    await api._async_upload_firmware(b"bix-bytes", "fw.bix")

    url = session.post.call_args.args[0]
    assert url.endswith("/cgi-bin/httpupload.cgi")
    assert "cmd=" not in url  # bare CGI, no query string
    headers = session.post.call_args.kwargs["headers"]
    assert headers["X-CSRF-XSID"] == "XSIDTOKEN"
    assert headers["X-Requested-With"] == "XMLHttpRequest"

    fields = parse_upload_payload(
        session.post.call_args.kwargs["data"], headers["Content-Type"]
    )
    names = [name for name, _ in fields]
    assert names == ["fileType", "xsrf", "imgName", "fileName"]
    values = dict(fields)
    assert values["fileType"] == "0"
    assert values["xsrf"] == "undefined"
    # imgName is the constant "1" (a standby indicator); "2" is rejected.
    assert values["imgName"] == "1"
    assert values["fileName"] == b"bix-bytes"
    # The status poll ran.
    api._authed_request.assert_awaited_with("get.cgi", "file_http_downloadStatus")


async def test_wait_for_upload_succeeds_after_uploading() -> None:
    """"uploading" keeps polling; "success" ends it."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(
        side_effect=[
            {"data": {"status": "uploading"}},
            {"data": {"status": "uploading"}},
            {"data": {"status": "success"}},
        ]
    )
    with patch("custom_components.netgear_poe.api.asyncio.sleep", new=AsyncMock()):
        await api._async_wait_for_upload()
    assert api._authed_request.await_count == 3


async def test_wait_for_upload_raises_on_failure_state() -> None:
    """A failure state ends the wait immediately, not at the timeout."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(return_value={"data": {"status": "failed"}})
    with (
        patch("custom_components.netgear_poe.api.asyncio.sleep", new=AsyncMock()),
        pytest.raises(NetgearError, match="Firmware write failed"),
    ):
        await api._async_wait_for_upload()
    assert api._authed_request.await_count == 1


# --- activate ---------------------------------------------------------------


async def test_activate_posts_file_dualconf_fields() -> None:
    """Activation posts file_dualConf with the constant imgName + descriptor."""
    api = NetgearPoeApi("host", "pw")
    api._authed_request = AsyncMock(return_value={"status": "ok"})

    await api._async_activate_image("1.0.5.12")

    cgi, cmd, body = api._authed_request.await_args.args
    assert (cgi, cmd) == ("set.cgi", "file_dualConf")
    assert "imgName=1" in body
    assert "imgDescriptor=1.0.5.12" in body
    assert "imgActive=on" in body
    assert "xsrf=undefined" in body


async def test_activate_uses_live_xsrf_on_aj4_backend() -> None:
    """The aj4 subclass carries its rotating xsrf token, not "undefined"."""
    api = NetgearJsonV2Api("host", "pw")
    api._xsrf = "LIVE-TOKEN"
    api._authed_request = AsyncMock(return_value={"status": "ok"})

    await api._async_activate_image("1.0.5.12")

    body = api._authed_request.await_args.args[2]
    assert "xsrf=LIVE-TOKEN" in body
    assert "xsrf=undefined" not in body


# --- reboot -----------------------------------------------------------------


async def test_reboot_posts_sys_reboot_and_clears_session() -> None:
    """Reboot posts sys_reboot(reboot=on) and drops the cached XSID."""
    api = NetgearPoeApi("host", "pw")
    api._xsid_header = "XSID"
    api._request = AsyncMock(return_value={"data": {"empty": 0}})

    await api._async_reboot()

    cgi, cmd, body = api._request.await_args.args
    assert (cgi, cmd) == ("set.cgi", "sys_reboot")
    assert "reboot=on" in body
    assert api._xsid_header is None


async def test_reboot_swallows_the_connection_drop() -> None:
    """Losing the connection is the reboot, not a failure."""
    api = NetgearPoeApi("host", "pw")
    api._xsid_header = "XSID"
    api._request = AsyncMock(side_effect=NetgearError("connection reset"))

    await api._async_reboot()  # must not raise
    assert api._xsid_header is None


# --- wait for firmware ------------------------------------------------------


async def test_wait_for_firmware_rides_out_reboot() -> None:
    """Old version, then down, then the new version -> success."""
    api = NetgearPoeApi("host", "pw")
    api.async_get_info = AsyncMock(
        side_effect=[
            SwitchInfo(name="s", model="GS310TP", firmware="1.0.1.2"),
            NetgearError("down"),
            SwitchInfo(name="s", model="GS310TP", firmware="1.0.5.12"),
        ]
    )
    progress: list[int] = []
    with patch("custom_components.netgear_poe.api.asyncio.sleep", new=AsyncMock()):
        await api._async_wait_for_firmware("1.0.5.12", progress.append)

    assert api.async_get_info.await_count == 3
    assert progress == sorted(progress)
    assert all(80 <= p <= 95 for p in progress)


async def test_wait_for_firmware_times_out_with_last_seen() -> None:
    """A stuck old version surfaces what the switch actually reports."""
    api = NetgearPoeApi("host", "pw")
    api.async_get_info = AsyncMock(
        return_value=SwitchInfo(name="s", model="GS310TP", firmware="1.0.1.2")
    )
    with (
        patch("custom_components.netgear_poe.api.asyncio.sleep", new=AsyncMock()),
        pytest.raises(NetgearError, match=r"last reported 1\.0\.1\.2"),
    ):
        await api._async_wait_for_firmware("1.0.5.12")


# --- full orchestration + fail-closed guards --------------------------------


async def test_install_uploads_verifies_activates_reboots() -> None:
    """Happy path: upload, confirm the inactive slot, activate, reboot, verify."""
    api = NetgearPoeApi("host", "pw")
    # running image1 (1.0.1.2); the switch writes image2 (the inactive slot).
    api.async_get_image_status = AsyncMock(
        side_effect=[
            _dis("1.0.1.2", "1.0.0.9", "image1"),
            _dis("1.0.1.2", "1.0.5.12", "image1"),
            _dis("1.0.1.2", "1.0.5.12", "image1", "image2"),
        ]
    )
    api._async_upload_firmware = AsyncMock()
    api._async_activate_image = AsyncMock()
    api._async_reboot = AsyncMock()
    api._async_wait_for_firmware = AsyncMock()
    progress: list[int] = []

    await api.async_install_firmware(
        b"bix", "1.0.5.12", filename="fw.bix", progress=progress.append
    )

    api._async_upload_firmware.assert_awaited_once_with(
        b"bix", "fw.bix", progress.append
    )
    api._async_activate_image.assert_awaited_once_with("1.0.5.12")
    api._async_reboot.assert_awaited_once()
    api._async_wait_for_firmware.assert_awaited_once_with("1.0.5.12", progress.append)
    assert progress == sorted(progress)
    assert progress[-1] == 100


async def test_install_stops_when_upload_not_recorded() -> None:
    """If neither slot took the new version, do not proceed."""
    api = NetgearPoeApi("host", "pw")
    unchanged = _dis("1.0.1.2", "1.0.0.9", "image1")
    api.async_get_image_status = AsyncMock(side_effect=[unchanged, unchanged])
    api._async_upload_firmware = AsyncMock()
    api._async_activate_image = AsyncMock()
    api._async_reboot = AsyncMock()

    with pytest.raises(NetgearError, match=r"no slot reports 1\.0\.5\.12"):
        await api.async_install_firmware(b"bix", "1.0.5.12", filename="fw.bix")

    api._async_activate_image.assert_not_awaited()
    api._async_reboot.assert_not_awaited()


async def test_install_ok_when_image_lands_in_active_slot() -> None:
    """Some firmware overwrites the ACTIVE slot; verify by version, not slot.

    Running image2; the upload lands in image2 (active) and the other slot
    keeps the older rollback. next-active already points at image2, so the
    version-based guards pass and the install completes.
    """
    api = NetgearPoeApi("host", "pw")
    api.async_get_image_status = AsyncMock(
        side_effect=[
            _dis("1.0.0.9", "1.0.1.2", "image2", "image2"),  # before
            _dis("1.0.0.9", "1.0.5.12", "image2", "image2"),  # after upload
            _dis("1.0.0.9", "1.0.5.12", "image2", "image2"),  # after activate
        ]
    )
    api._async_upload_firmware = AsyncMock()
    api._async_activate_image = AsyncMock()
    api._async_reboot = AsyncMock()
    api._async_wait_for_firmware = AsyncMock()

    await api.async_install_firmware(b"bix", "1.0.5.12", filename="fw.bix")
    api._async_reboot.assert_awaited_once()


async def test_install_stops_when_activation_did_not_stick() -> None:
    """next-active must move to the target slot before any reboot."""
    api = NetgearPoeApi("host", "pw")
    api.async_get_image_status = AsyncMock(
        side_effect=[
            _dis("1.0.1.2", "1.0.0.9", "image1"),
            _dis("1.0.1.2", "1.0.5.12", "image1"),
            # next-active never moved to image2: activation did not stick.
            _dis("1.0.1.2", "1.0.5.12", "image1"),
        ]
    )
    api._async_upload_firmware = AsyncMock()
    api._async_activate_image = AsyncMock()
    api._async_reboot = AsyncMock()

    with pytest.raises(NetgearError, match="boot image"):
        await api.async_install_firmware(b"bix", "1.0.5.12", filename="fw.bix")

    api._async_reboot.assert_not_awaited()
