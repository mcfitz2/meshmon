import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from meshmon import link, main
from meshmon.config import Config
from meshmon.link import LinkDownError


class Stop(Exception):
    pass


async def stop(_seconds: float) -> None:
    raise Stop


def test_the_companion_is_down_when_it_cannot_be_reached(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def unreachable(host: str, port: int) -> AsyncIterator[Any]:
        raise LinkDownError("x")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=stop))
    monkeypatch.setattr(link, "connect", unreachable)
    registry = CollectorRegistry()

    with pytest.raises(Stop):
        asyncio.run(main.run(Config(), registry))

    assert registry.get_sample_value("meshmon_companion_up") == 0


def test_the_companion_is_up_after_a_round(monkeypatch: pytest.MonkeyPatch) -> None:
    @asynccontextmanager
    async def reachable(host: str, port: int) -> AsyncIterator[Any]:
        yield object()

    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=stop))
    monkeypatch.setattr(link, "connect", reachable)
    registry = CollectorRegistry()

    with pytest.raises(Stop):
        asyncio.run(main.run(Config(), registry))

    assert registry.get_sample_value("meshmon_companion_up") == 1


def test_an_unexpected_failure_marks_the_companion_down_and_keeps_looping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    @asynccontextmanager
    async def broken(host: str, port: int) -> AsyncIterator[Any]:
        raise RuntimeError("boom")
        yield  # pragma: no cover

    monkeypatch.setattr(main, "asyncio", SimpleNamespace(sleep=stop))
    monkeypatch.setattr(link, "connect", broken)
    registry = CollectorRegistry()

    with pytest.raises(Stop):
        asyncio.run(main.run(Config(), registry))

    assert registry.get_sample_value("meshmon_companion_up") == 0
