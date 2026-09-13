"""Thumbnail of a bot's screen: one JPEG grab of the profile's Xvnc display.

Feeds the Screen hero in Hermes Desktop. ``thumbnail_data_url`` is the raw grab (no lease
write). ``thumbnail_for_clients`` applies the same admission/epoch fence as ``computer_use``
capture so a takeover mid-grab cannot ship the frame.
"""

from __future__ import annotations

import base64
import io
import os
import threading
from typing import Optional

from tools.bot_desktop import lease, runtime

THUMB_MAX = (960, 600)
_grab_lock = threading.Lock()


def thumbnail_for_clients(max_size: tuple[int, int] = THUMB_MAX, quality: int = 72) -> dict:
    """JPEG grab for Desktop clients, fenced the same way as ``computer_use`` capture.

    A pre-check alone is not enough: ImageGrab can take seconds, and a takeover in that
    window would ship whatever the human typed. Any control-epoch change (including a
    finished take-over / hand-back cycle) discards the frame.
    """
    admitted = lease.get()
    if admitted.holder == lease.HUMAN:
        return {"data_url": None, "suppressed": "human_has_control"}
    data_url = thumbnail_data_url(max_size=max_size, quality=quality)
    now = lease.get()
    if now.holder == lease.HUMAN or now.epoch != admitted.epoch:
        return {"data_url": None, "suppressed": "human_has_control"}
    return {"data_url": data_url}


def thumbnail_data_url(max_size: tuple[int, int] = THUMB_MAX, quality: int = 72) -> Optional[str]:
    """``data:image/jpeg;base64,...`` of the running screen, or ``None`` when no screen is up."""
    env = runtime.published_env()
    display = env.get("DISPLAY")
    if not display or runtime._launcher_pid() is None:
        return None
    from PIL import ImageGrab  # Pillow is a hard dependency; import lazily to keep status calls cheap

    # Xlib reads XAUTHORITY from the process env; the launcher publishes a per-profile cookie file.
    # The swap is process-wide, so two profiles grabbed on worker threads at once serialise here or
    # one would grab with the other's cookie and restore the wrong value.
    with _grab_lock:
        previous = os.environ.get("XAUTHORITY")
        if env.get("XAUTHORITY"):
            os.environ["XAUTHORITY"] = env["XAUTHORITY"]
        try:
            image = ImageGrab.grab(xdisplay=display)
        finally:
            if previous is None:
                os.environ.pop("XAUTHORITY", None)
            else:
                os.environ["XAUTHORITY"] = previous
    image.thumbnail(max_size)
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality, optimize=True)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode("ascii")
