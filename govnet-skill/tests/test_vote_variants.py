"""EMGVote 双形态开关 — 验证两种 typed data 都能本地复算摘要。"""

import os

import pytest

from lib import sign as sign_mod
from lib.sign import (
    EMG_VOTE_TYPES_MAIN_SPEC,
    EMG_VOTE_TYPES_OPENAPI,
    build_emg_vote_typed_data,
    compute_eip712_digest,
)


AUTH_INFO = {
    "eip712_domain": {
        "name": "EMG",
        "version": "1",
        "chainId": 56,
        "verifyingContract": "0x" + "aa" * 20,
    }
}


@pytest.fixture
def main_spec_env(monkeypatch):
    monkeypatch.delenv("GOVNET_VOTE_TYPED_DATA_VARIANT", raising=False)


@pytest.fixture
def openapi_env(monkeypatch):
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "openapi")


def test_main_spec_variant_includes_principal(main_spec_env):
    typed = build_emg_vote_typed_data(
        principal="0x" + "42" * 20,
        epoch=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        nonce=1,
        auth_info=AUTH_INFO,
    )
    assert typed["types"]["EMGVote"] == EMG_VOTE_TYPES_MAIN_SPEC["EMGVote"]
    assert "principal" in typed["message"]
    assert typed["message"]["principal"] == "0x" + "42" * 20
    # 摘要可计算（不抛）
    digest = compute_eip712_digest(typed)
    assert len(digest) == 32


def test_openapi_variant_omits_principal(openapi_env):
    typed = build_emg_vote_typed_data(
        principal="0x" + "42" * 20,  # 会被静默丢弃
        epoch=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        nonce=1,
        auth_info=AUTH_INFO,
    )
    assert typed["types"]["EMGVote"] == EMG_VOTE_TYPES_OPENAPI["EMGVote"]
    assert "principal" not in typed["message"]
    # 摘要可计算（不抛）
    digest = compute_eip712_digest(typed)
    assert len(digest) == 32


def test_two_variants_produce_different_digests(monkeypatch):
    """同样 (epoch, voteHash, predictionHash, nonce) 在两个形态下摘要必须不同。

    否则切换形态没意义 — 这条测试确保 typed data 真的因为 type string 不同
    而走到不同的 hashStruct。
    """
    args = dict(
        principal="0x" + "42" * 20,
        epoch=6,
        vote_hash=b"\x11" * 32,
        prediction_hash=b"\x22" * 32,
        nonce=1,
        auth_info=AUTH_INFO,
    )
    monkeypatch.delenv("GOVNET_VOTE_TYPED_DATA_VARIANT", raising=False)
    main_digest = compute_eip712_digest(build_emg_vote_typed_data(**args))
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "openapi")
    openapi_digest = compute_eip712_digest(build_emg_vote_typed_data(**args))
    assert main_digest != openapi_digest


def test_default_variant_is_main_spec(monkeypatch):
    monkeypatch.delenv("GOVNET_VOTE_TYPED_DATA_VARIANT", raising=False)
    assert sign_mod._vote_types() is EMG_VOTE_TYPES_MAIN_SPEC


def test_unknown_variant_falls_back_to_main_spec(monkeypatch):
    monkeypatch.setenv("GOVNET_VOTE_TYPED_DATA_VARIANT", "garbage")
    assert sign_mod._vote_types() is EMG_VOTE_TYPES_MAIN_SPEC
