"""目标直播间身份核验与错房阻断判定器 (Ticket 09)。"""
from enum import Enum
from dataclasses import dataclass
import logging
from typing import Optional

logger = logging.getLogger("IdentityVerifier")


class IdentityStatus(str, Enum):
    """身份核验三态判定。"""
    VERIFIED = "VERIFIED"        # 身份匹配通过
    MISMATCH = "MISMATCH"        # 确诊错房（绝对不可放行）
    UNCONFIRMED = "UNCONFIRMED"  # 证据不足无法确认


@dataclass
class IdentityVerificationResult:
    """身份核验判定结果。"""
    status: IdentityStatus
    can_proceed: bool
    reason: str
    target_uid: Optional[str] = None
    current_uid: Optional[str] = None
    target_nickname: Optional[str] = None
    current_nickname: Optional[str] = None


def verify_room_identity(
    target_uid: Optional[str],
    current_uid: Optional[str],
    target_nickname: Optional[str] = None,
    current_nickname: Optional[str] = None,
    allow_unconfirmed: bool = False,
) -> IdentityVerificationResult:
    """执行严格主播身份一致性校验。

    核心裁决矩阵：
    1. 稳定标识（UID）明确不匹配：无论是否开启 allow_unconfirmed，唯一判定为 MISMATCH，绝不放行；
    2. 稳定标识匹配：唯一判定为 VERIFIED，放行（主播改名不影响匹配）；
    3. 稳定标识不可获得：唯一判定为 UNCONFIRMED。
       - 严格模式 (allow_unconfirmed=False): 阻断；
       - 逃逸模式 (allow_unconfirmed=True): 放行但打上警告标记。
    """
    t_uid = target_uid.strip() if target_uid else ""
    c_uid = current_uid.strip() if current_uid else ""

    # 1. 双方均有稳定同类数字标识
    if t_uid and c_uid:
        if t_uid == c_uid:
            # 稳定标识一致，即使昵称改变也放行
            if target_nickname and current_nickname and target_nickname != current_nickname:
                logger.info(f"主播 UID {t_uid} 匹配，检测到昵称变更: '{target_nickname}' -> '{current_nickname}'")
            return IdentityVerificationResult(
                status=IdentityStatus.VERIFIED,
                can_proceed=True,
                reason="uid_matched",
                target_uid=t_uid,
                current_uid=c_uid,
                target_nickname=target_nickname,
                current_nickname=current_nickname,
            )
        else:
            # 双方 UID 明确不相等 -> 确诊错房！硬规则阻断！
            logger.error(
                f"【严重错房警报】当前主播 UID ({c_uid}) 与目标主播 UID ({t_uid}) 明确不一致！"
                f"即使前台展示相同昵称，也坚决阻断退出！"
            )
            return IdentityVerificationResult(
                status=IdentityStatus.MISMATCH,
                can_proceed=False,
                reason="id_mismatch",
                target_uid=t_uid,
                current_uid=c_uid,
                target_nickname=target_nickname,
                current_nickname=current_nickname,
            )

    # 2. 稳定标识不可获得 (缺失或未能读取)
    logger.warning("未能可靠取得主播稳定数字 UID，进入无法确认 (UNCONFIRMED) 逻辑分支。")
    if allow_unconfirmed:
        logger.warning("已开启 --allow-unconfirmed-identity 逃逸开关，允许临时录制，但心跳与事件需显式标记风险。")
        return IdentityVerificationResult(
            status=IdentityStatus.UNCONFIRMED,
            can_proceed=True,
            reason="unconfirmed_allowed_by_flag",
            target_uid=t_uid or None,
            current_uid=c_uid or None,
            target_nickname=target_nickname,
            current_nickname=current_nickname,
        )
    else:
        logger.error("严格模式下主播身份未获证实，拒绝盲目录制。")
        return IdentityVerificationResult(
            status=IdentityStatus.UNCONFIRMED,
            can_proceed=False,
            reason="identity_unconfirmed",
            target_uid=t_uid or None,
            current_uid=c_uid or None,
            target_nickname=target_nickname,
            current_nickname=current_nickname,
        )
