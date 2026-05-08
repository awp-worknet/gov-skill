"""EMGVote 三形态开关 — 验证 latest_2026_05 (default) / main_spec / openapi
都能本地复算摘要，且两两形态摘要不同（type string 真的进了 hashStruct）。
"""

import pytest

from lib import sign as sign_mod
from lib.sign import (
    EMG_VOTE_TYPES_LATEST_2026_05,
    EMG_VOTE_TYPES_MAIN_SPEC,
    EMG_VOTE_TYPES_OPENAPI,
    build_emg_vote_typed_data,
    compute_eip712_digest,
)


AUTH_INFO = {
    "eip712_domain": {
        "name": "EMG",
        "version": "1",
        "chainId": 8453,
        "verifyingContract": "0x" + "aa" * 20,
    }
}


@pytest.fixture
def latest_env(monkeypatch):
    monkeypatch.delenv("GOVNET_VOTE_TYPED_DATA_VARIANT", raising=False)


@pytest.fixture
def main_spec_env(monkeypatch):
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "main_spec")


@pytest.fixture
def openapi_env(monkeypatch):
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "openapi")


# --- latest_2026_05（当前生产，默认）-----------------------------------------


def test_latest_variant_has_six_fields(latest_env):
    typed = build_emg_vote_typed_data(
        principal="0x" + "42" * 20,
        market_id=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        vote_revision=3,
        timestamp=1_775_000_000,
        auth_info=AUTH_INFO,
    )
    assert typed["types"]["EMGVote"] == EMG_VOTE_TYPES_LATEST_2026_05["EMGVote"]
    msg = typed["message"]
    assert msg["principal"] == "0x" + "42" * 20
    assert msg["market_id"] == "6"
    assert msg["vote_revision"] == "3"
    assert msg["vote_hash"] == "0x" + "11" * 32
    assert msg["prediction_hash"] == "0x" + "22" * 32
    assert msg["timestamp"] == "1775000000"
    digest = compute_eip712_digest(typed)
    assert len(digest) == 32


def test_latest_variant_requires_timestamp(latest_env):
    with pytest.raises(ValueError, match=r"latest_2026_05 EMGVote requires timestamp"):
        build_emg_vote_typed_data(
            principal="0x" + "42" * 20,
            market_id=6,
            vote_hash=b"\x11" * 32,
            prediction_hash=b"\x22" * 32,
            vote_revision=1,
            auth_info=AUTH_INFO,
        )


def test_default_variant_is_latest(monkeypatch):
    monkeypatch.delenv("GOVNET_VOTE_TYPED_DATA_VARIANT", raising=False)
    assert sign_mod._vote_types() is EMG_VOTE_TYPES_LATEST_2026_05


def test_unknown_variant_falls_back_to_latest(monkeypatch):
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "garbage")
    assert sign_mod._vote_types() is EMG_VOTE_TYPES_LATEST_2026_05


# --- main_spec（旧 5 字段） --------------------------------------------------


def test_main_spec_variant_uses_epoch_nonce_camelcase(main_spec_env):
    typed = build_emg_vote_typed_data(
        principal="0x" + "42" * 20,
        market_id=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        vote_revision=1,
        timestamp=1_775_000_000,  # 旧形态会忽略
        auth_info=AUTH_INFO,
    )
    assert typed["types"]["EMGVote"] == EMG_VOTE_TYPES_MAIN_SPEC["EMGVote"]
    msg = typed["message"]
    assert msg["principal"] == "0x" + "42" * 20
    assert msg["epoch"] == "6"
    assert msg["nonce"] == "1"
    assert msg["voteHash"] == "0x" + "11" * 32
    assert "timestamp" not in msg
    assert "market_id" not in msg
    assert "vote_revision" not in msg


def test_main_spec_accepts_legacy_kwargs(main_spec_env):
    """旧调用方传 epoch=/nonce= 应当继续工作。"""
    typed = build_emg_vote_typed_data(
        principal="0x" + "42" * 20,
        market_id=None,  # 显式 None
        epoch=6,         # 旧 alias
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        vote_revision=None,
        nonce=7,         # 旧 alias
        auth_info=AUTH_INFO,
    )
    assert typed["message"]["epoch"] == "6"
    assert typed["message"]["nonce"] == "7"


# --- openapi（旧 4 字段） ----------------------------------------------------


def test_openapi_variant_omits_principal(openapi_env):
    typed = build_emg_vote_typed_data(
        principal="0x" + "42" * 20,
        market_id=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        vote_revision=1,
        timestamp=1_775_000_000,
        auth_info=AUTH_INFO,
    )
    assert typed["types"]["EMGVote"] == EMG_VOTE_TYPES_OPENAPI["EMGVote"]
    msg = typed["message"]
    assert "principal" not in msg
    assert msg["epoch"] == "6"
    assert msg["nonce"] == "1"


# --- 三形态摘要必须两两不同 -------------------------------------------------


def test_three_variants_produce_three_distinct_digests(monkeypatch):
    """同样 (market, vote_hash, pred_hash, revision) 在三个形态下摘要必须两两不同。"""
    args = dict(
        principal="0x" + "42" * 20,
        market_id=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        vote_revision=1,
        timestamp=1_775_000_000,
        auth_info=AUTH_INFO,
    )
    monkeypatch.delenv("GOVNET_VOTE_TYPED_DATA_VARIANT", raising=False)
    latest_digest = compute_eip712_digest(build_emg_vote_typed_data(**args))
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "main_spec")
    main_digest = compute_eip712_digest(build_emg_vote_typed_data(**args))
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "openapi")
    openapi_digest = compute_eip712_digest(build_emg_vote_typed_data(**args))
    assert latest_digest != main_digest
    assert main_digest != openapi_digest
    assert latest_digest != openapi_digest
