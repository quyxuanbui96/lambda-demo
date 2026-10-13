"""Scheduled outbox/lease recovery and upload cleanup."""

from __future__ import annotations

from collections.abc import Mapping

from src.workflow.runtime import get_runtime


def handler(event: Mapping[str, object], context: object) -> dict[str, object]:
    runtime = get_runtime()
    return {**runtime.maintenance.recover(), **runtime.maintenance.cleanup()}
