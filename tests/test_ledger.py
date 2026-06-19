"""Ledger content_hash contract — the Python half of the Python/Rust parity
guarantee (ADR-0002).

The cross-language hash test lives in opsctl (it shells out to Python). These
tests pin the same invariants from the Python gate so a regression in
``_canonical`` is caught by ``pytest``, not only by ``cargo test``:

  - determinism against hardcoded vectors (identical to the Rust test vectors)
  - non-ASCII stability (ensure_ascii escaping)
  - the no-floats / no-NaN contract that keeps the hash byte-compatible
"""

from __future__ import annotations

import math

import pytest

from sabnyx import ledger


# These expected values are byte-identical to the hardcoded vectors asserted in
# opsctl/src/ledger.rs (test_hash_*). If either side changes, both must change.
@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"task": "tend", "status": "started"},
            "sha256:a1f2b81593a4865bd65cc621c82d60b87042fd86963fe94de77ccbe21093d2fe",
        ),
        (
            {"a": 1, "b": [1, 2, 3], "nested": {"z": 1, "y": 2}},
            "sha256:7b72446e66532f78b25981b5ac9df976562d763d83d37f4436ce673289b8c4d4",
        ),
        ({}, "sha256:44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a"),
    ],
)
def test_content_hash_matches_rust_vectors(payload: dict, expected: str) -> None:
    assert ledger.content_hash(payload) == expected


def test_content_hash_is_deterministic_and_key_order_independent() -> None:
    a = ledger.content_hash({"x": 1, "y": {"b": 2, "a": 3}})
    b = ledger.content_hash({"y": {"a": 3, "b": 2}, "x": 1})
    assert a == b


def test_content_hash_unicode_is_stable() -> None:
    # ensure_ascii=True path: BMP + supplementary (emoji) char.
    h = ledger.content_hash({"k": "café 日本語 🎉"})
    assert h.startswith("sha256:")
    assert h == ledger.content_hash({"k": "café 日本語 🎉"})


@pytest.mark.parametrize(
    "payload",
    [
        {"cost": 1.5},
        {"nested": {"v": 0.1}},
        {"items": [1, 2, 3.0]},
        {"big": 1e20},
    ],
)
def test_content_hash_rejects_floats(payload: dict) -> None:
    with pytest.raises(TypeError, match="float"):
        ledger.content_hash(payload)


@pytest.mark.parametrize("bad", [math.nan, math.inf, -math.inf])
def test_content_hash_rejects_non_finite(bad: float) -> None:
    with pytest.raises(TypeError, match="float"):
        ledger.content_hash({"v": bad})


def test_bools_are_allowed() -> None:
    # bool is a subclass of int but is NOT a float — must pass.
    h = ledger.content_hash({"ok": True, "no": False})
    assert h.startswith("sha256:")


def test_append_event_rejects_float_payload(tmp_vault) -> None:
    with pytest.raises(TypeError, match="float"):
        ledger.append_event(kind="test.event", actor="t", subject="s", payload={"ratio": 0.5})
