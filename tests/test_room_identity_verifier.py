"""测试目标房间身份核验闭环与错房阻断规则 (Ticket 09)。"""
import pytest

from src.worker.identity_verifier import (
    IdentityStatus,
    verify_room_identity,
    IdentityVerificationResult,
)


def test_identity_verifier_mismatch_blocks_even_with_allow_unconfirmed():
    """双方数字 UID 明确不一致（确诊错房）时，无论是否带逃逸参数，必须判定 MISMATCH 且绝不放行。"""
    res = verify_room_identity(
        target_uid="12345",
        current_uid="67890",
        target_nickname="相同昵称主播",
        current_nickname="相同昵称主播",
        allow_unconfirmed=True,  # 故意开启逃逸开关
    )
    assert res.status == IdentityStatus.MISMATCH
    assert res.can_proceed is False
    assert res.reason == "id_mismatch"


def test_identity_verifier_unconfirmed_handling_strict_vs_escape():
    """数字 UID 缺失但昵称一致时，判定为 UNCONFIRMED。严格模式阻断，逃逸模式放行但带标记。"""
    # 1. 严格模式 (allow_unconfirmed=False)
    strict_res = verify_room_identity(
        target_uid=None,
        current_uid=None,
        target_nickname="主播小A",
        current_nickname="主播小A",
        allow_unconfirmed=False,
    )
    assert strict_res.status == IdentityStatus.UNCONFIRMED
    assert strict_res.can_proceed is False
    assert strict_res.reason == "identity_unconfirmed"

    # 2. 逃逸模式 (allow_unconfirmed=True)
    escape_res = verify_room_identity(
        target_uid=None,
        current_uid=None,
        target_nickname="主播小A",
        current_nickname="主播小A",
        allow_unconfirmed=True,
    )
    assert escape_res.status == IdentityStatus.UNCONFIRMED
    assert escape_res.can_proceed is True
    assert escape_res.reason == "unconfirmed_allowed_by_flag"


def test_identity_verifier_verified_when_uid_matches_even_nickname_changed():
    """数字 UID 匹配时，即使前台昵称改变，必须判定为 VERIFIED 并放行。"""
    res = verify_room_identity(
        target_uid="12345",
        current_uid="12345",
        target_nickname="原昵称",
        current_nickname="改名后的新昵称【直播中】",
        allow_unconfirmed=False,
    )
    assert res.status == IdentityStatus.VERIFIED
    assert res.can_proceed is True
