"""Camera stills: the single-frame route on ODIN 2.2.0+, the stream's first frame on older servers."""
from homeassistant.components.camera import async_get_image
from homeassistant.const import CONF_API_KEY, CONF_URL
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.odin.const import CONF_SCAN_INTERVAL, DOMAIN

from .conftest import STATUS_URL, URL

SNAPSHOT = URL + "/api/overlay/1/snapshot?token=odin_cam"
STREAM = URL + "/api/overlay/1/camera?token=odin_cam"
JPEG = b"\xff\xd8\xff\xe0" + b"x" * 64 + b"\xff\xd9"
MJPEG = b"--frame\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n--frame\r\n"


async def _camera(hass, aioclient_mock, status):
    aioclient_mock.get(STATUS_URL, json=status)
    entry = MockConfigEntry(domain=DOMAIN, unique_id=URL.lower(), data={CONF_URL: URL, CONF_API_KEY: "odin_key"},
                            options={CONF_SCAN_INTERVAL: 20, "camera_token": "odin_cam"})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


def _calls(aioclient_mock, path):
    return [c for c in aioclient_mock.mock_calls if path in str(c[1])]


async def test_snapshot_route_is_used_and_no_stream_is_opened(hass, aioclient_mock, status):
    await _camera(hass, aioclient_mock, status)
    aioclient_mock.get(SNAPSHOT, content=JPEG)
    aioclient_mock.get(STREAM, content=MJPEG)
    image = await async_get_image(hass, "camera.x1c_camera")
    assert image.content == JPEG
    assert len(_calls(aioclient_mock, "/snapshot")) == 1 and not _calls(aioclient_mock, "/api/overlay/1/camera")


async def test_older_server_without_the_route_falls_back_to_the_stream_and_remembers(hass, aioclient_mock, status):
    await _camera(hass, aioclient_mock, status)
    aioclient_mock.get(SNAPSHOT, status=404, json={"detail": "Not Found"})
    aioclient_mock.get(STREAM, content=MJPEG)
    assert (await async_get_image(hass, "camera.x1c_camera")).content == JPEG
    assert (await async_get_image(hass, "camera.x1c_camera")).content == JPEG
    assert len(_calls(aioclient_mock, "/snapshot")) == 1          # not asked again right away
    assert len(_calls(aioclient_mock, "/api/overlay/1/camera")) == 2


async def test_a_proxy_page_or_app_fallback_is_not_taken_for_a_picture(hass, aioclient_mock, status):
    await _camera(hass, aioclient_mock, status)
    aioclient_mock.get(SNAPSHOT, text="<html>app</html>")           # 200, but not a JPEG
    aioclient_mock.get(STREAM, content=MJPEG)
    assert (await async_get_image(hass, "camera.x1c_camera")).content == JPEG
    assert len(_calls(aioclient_mock, "/api/overlay/1/camera")) == 1


async def test_a_missing_camera_on_a_new_server_does_not_fall_back(hass, aioclient_mock, status):
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    await _camera(hass, aioclient_mock, status)
    aioclient_mock.get(SNAPSHOT, status=404, json={"detail": "Camera not found"})
    aioclient_mock.get(STREAM, content=MJPEG)
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, "camera.x1c_camera")
    assert not _calls(aioclient_mock, "/api/overlay/1/camera")


async def test_an_oversized_snapshot_with_no_length_is_dropped_before_it_is_buffered_whole(hass, aioclient_mock, status):
    """Regression (Codex sign-off 11-12): the whole body was read before the size was judged."""
    import pytest
    from homeassistant.exceptions import HomeAssistantError
    from custom_components.odin import camera
    await _camera(hass, aioclient_mock, status)
    aioclient_mock.get(SNAPSHOT, content=JPEG)
    assert (await async_get_image(hass, "camera.x1c_camera")).content == JPEG      # the route is known to exist
    aioclient_mock.clear_requests()
    aioclient_mock.get(SNAPSHOT, content=b"\xff\xd8" + b"x" * (camera.SNAPSHOT_MAX_BYTES + 200_000))
    with pytest.raises(HomeAssistantError):
        await async_get_image(hass, "camera.x1c_camera")
