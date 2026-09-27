"""P5-T3 test 1: the replay clock is a monotonic, UTC-aware `Clock`."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from trader.market.clock import Clock
from trader.replay.clock import ReplayClock

START = datetime(2026, 9, 21, 13, 30, tzinfo=UTC)


def test_satisfies_clock_protocol() -> None:
    clock: Clock = ReplayClock(START)  # typed assignment, checked by mypy
    assert clock.now() == START


def test_set_and_advance_move_forward() -> None:
    clock = ReplayClock(START)
    clock.set(START + timedelta(minutes=5))
    assert clock.now() == START + timedelta(minutes=5)
    clock.set(START + timedelta(minutes=5))  # the same time again is allowed (bars then events at t)
    assert clock.now() == START + timedelta(minutes=5)
    clock.advance(timedelta(minutes=1))
    assert clock.now() == START + timedelta(minutes=6)
    clock.advance(timedelta(0))
    assert clock.now() == START + timedelta(minutes=6)


def test_now_is_utc_even_for_another_zone() -> None:
    mt = timezone(timedelta(hours=-6))
    clock = ReplayClock(START.astimezone(mt))
    assert clock.now().tzinfo == UTC
    assert clock.now() == START
    clock.set((START + timedelta(minutes=1)).astimezone(mt))
    assert clock.now().tzinfo == UTC
    assert clock.now() == START + timedelta(minutes=1)


def test_refuses_naive_datetimes() -> None:
    with pytest.raises(ValueError):
        ReplayClock(datetime(2026, 9, 21, 13, 30))
    clock = ReplayClock(START)
    with pytest.raises(ValueError):
        clock.set(datetime(2026, 9, 21, 13, 31))
    assert clock.now() == START


def test_refuses_to_go_backwards() -> None:
    clock = ReplayClock(START + timedelta(minutes=10))
    with pytest.raises(ValueError):
        clock.set(START + timedelta(minutes=9, seconds=59))
    assert clock.now() == START + timedelta(minutes=10)
    with pytest.raises(ValueError):
        clock.advance(timedelta(microseconds=-1))
    assert clock.now() == START + timedelta(minutes=10)
