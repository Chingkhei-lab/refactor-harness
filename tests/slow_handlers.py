"""SIGTERM test support: a handler slow enough to observe a mid-job drain."""

from __future__ import annotations

import time
from typing import Any

from engine.worker import HandlerContext


def slow_handler(payload: dict[str, Any], ctx: HandlerContext) -> dict[str, Any]:
    time.sleep(2.0)
    return {"ok": True}


HANDLERS = {"slow": slow_handler}
