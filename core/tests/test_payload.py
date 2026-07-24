"""Tests for the cheap size probe (orrery_core.payload)."""

from __future__ import annotations

import json
import time

from orrery_core.payload import text_volume


def test_counts_strings_and_keys():
    assert text_volume({"ab": "cde"}) == 5


def test_walks_nested_structures():
    obj = {"a": ["one", {"b": "two"}], "c": ("three",)}
    # keys a,b,c = 3; values 3 + 3 + 5 = 11
    assert text_volume(obj) == 14


def test_ignores_scalars():
    assert text_volume({"n": 12345, "f": 1.5, "b": True, "z": None}) == 4


def test_handles_a_bare_string_and_scalars():
    assert text_volume("hello") == 5
    assert text_volume(42) == 0


def test_limit_short_circuits():
    big = {"logs": "x" * 10_000}
    assert text_volume(big, limit=100) >= 100


def test_survives_deep_nesting_without_recursing():
    obj: dict = {"leaf": "x"}
    for _ in range(200):
        obj = {"nested": obj}
    text_volume(obj)  # must not raise RecursionError


def test_survives_a_self_referential_structure():
    """Depth-bounded, so a cycle terminates instead of hanging."""
    obj: dict = {"name": "loop"}
    obj["self"] = obj
    text_volume(obj)


def test_is_much_cheaper_than_serializing():
    """The reason this exists: deciding whether a payload is big must not cost
    as much as the work being decided about."""
    payload = {"status": "success", "logs": "line of text\n" * 400_000}

    start = time.perf_counter()
    text_volume(payload)
    probe = time.perf_counter() - start

    start = time.perf_counter()
    len(json.dumps(payload))
    serialize = time.perf_counter() - start

    assert probe < serialize / 10


def test_approximates_the_serialized_size():
    payload = {"status": "success", "logs": "x" * 50_000}
    volume = text_volume(payload)
    serialized = len(json.dumps(payload))
    # Within the punctuation overhead — close enough for a threshold.
    assert 0.9 < volume / serialized < 1.1
