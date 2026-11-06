"""目标直播间定位：以实际页面证据推进导航，不把 Intent 返回成功当作到达。"""
from dataclasses import dataclass
import re
import time
from urllib.parse import urlsplit, parse_qs

from src.worker.identity_verifier import IdentityStatus


@dataclass(frozen=True)
class RoomEvidence:
    room_id: str | None = None
    anchor_uid: str | None = None
    nickname: str | None = None
    source: str = "unavailable"
    activity_token: str | None = None
    observed_at: float = 0.0


@dataclass(frozen=True)
class LandingResult:
    success: bool
    reason: str
    status: IdentityStatus = IdentityStatus.UNCONFIRMED
    evidence: RoomEvidence | None = None
    attempts: int = 0


def activity_records(dump: str) -> list[dict]:
    """只解析真实任务栈 Hist 条目，不采信重复的摘要或任意日志中的 URI。"""
    pattern = re.compile(r'\* Hist\s+#\d+:\s+ActivityRecord\{(\S+)\s+u\d+\s+(\S+)\s+t(\d+)[^}]*\}')
    matches = list(pattern.finditer(dump))
    return [dict(token=m.group(1), component=m.group(2), task=m.group(3),
                 body=dump[m.end():matches[i+1].start() if i+1<len(matches) else len(dump)])
            for i,m in enumerate(matches)]


def fresh_profile_evidence(before: str, after: str, live_task: str,
                           nickname: str | None = None) -> RoomEvidence | None:
    """从点击当前主播头像后新建的资料 Activity 中取证，不接收目标参数。

    客户端生成的 user/profile URI 含 uid 和 room_id；必须是同一直播任务中
    新出现的 Activity、previous_page=live，不能复用历史资料页或目标导航 URI。
    """
    seen = {r['token'] for r in activity_records(before)}
    candidates = []
    for record in activity_records(after):
        if (record['token'] in seen or record['task'] != live_task
                or record['component'] not in {
                    'com.ss.android.ugc.aweme/.profile.ui.UserProfileActivity',
                    'com.ss.android.ugc.aweme/com.ss.android.ugc.aweme.profile.ui.UserProfileActivity'}):
            continue
        match = re.search(r'\bdat=(aweme://user/profile\?[^\s}]+)', record['body'])
        if not match:
            continue
        query = parse_qs(urlsplit(match.group(1)).query)
        def single(key):
            values = query.get(key, [])
            return values[0] if len(values) == 1 else None
        if single('previous_page') != 'live' and single('extra_previous_page') != 'live':
            continue
        uid, room_id, anchor = single('uid'), single('room_id'), single('anchor_id')
        if not uid or not room_id or not uid.isdigit() or not room_id.isdigit():
            continue
        if anchor is not None and anchor != uid:
            continue
        candidates.append(RoomEvidence(room_id, uid, nickname, 'fresh_profile_intent',
                                       record['token'], time.time()))
    return candidates[0] if len(candidates) == 1 else None


def compare_evidence(evidence: RoomEvidence, room_id: str | None,
                     anchor_uid: str | None) -> IdentityStatus:
    """比较同类稳定标识；昵称只展示，不用同名文本替代身份匹配。"""
    pairs = [(room_id, evidence.room_id), (anchor_uid, evidence.anchor_uid)]
    comparable = [(str(a), str(b)) for a,b in pairs if a and b]
    if any(a != b for a,b in comparable):
        return IdentityStatus.MISMATCH
    return IdentityStatus.VERIFIED if comparable else IdentityStatus.UNCONFIRMED


def locate_target(navigator, target: str, *, target_kind: str = 'auto',
                  web_rid: str | None = None, anchor_uid: str | None = None,
                  timeout_sec: float = 100, max_attempts: int = 3,
                  stop_requested=lambda: False, on_step=lambda **kw: None,
                  allow_unconfirmed: bool = False) -> LandingResult:
    """定向进入→观察→必要时换入口/复位→取证→恢复直播画面→开始录制。"""
    deadline = time.monotonic() + timeout_sec
    internal = target_kind == 'room_id' or (target_kind == 'auto' and bool(re.fullmatch(r'\d{19}', target)))
    expected_room = target if internal else None
    last_reason, last_status, last_evidence = 'navigation_failed', IdentityStatus.UNCONFIRMED, None
    attempted = 0
    for attempt in range(1, max_attempts + 1):
        if stop_requested():
            return LandingResult(False, 'cancelled', attempts=attempted)
        if time.monotonic() >= deadline:
            break
        attempted = attempt
        route = 'primary' if attempt == 1 else ('web' if attempt == 2 and web_rid else 'reset')
        # 明确错房先退出错误页面，再定向进入；不反复检查同一错误画面。
        if last_status == IdentityStatus.MISMATCH:
            route = 'reset'
        on_step(attempt=attempt, action='navigate', route=route, reason=last_reason)
        if not navigator.navigate_route(target, web_rid, route, deadline, stop_requested):
            last_reason = 'intent_failed'
            continue
        if not navigator.wait_live_page(deadline=min(deadline, time.monotonic()+12), stop_requested=stop_requested):
            last_reason = navigator.last_gate_reason
            on_step(attempt=attempt, action='retry', route=route, reason=last_reason)
            continue
        # Live Activity 已出现不代表视频/头部资料入口已经完成加载。
        on_step(attempt=attempt, action='wait_initial_picture', route=route, reason='wait_render')
        if not navigator.wait_live_picture(deadline=min(deadline, time.monotonic()+16), stop_requested=stop_requested):
            last_reason = navigator.last_gate_reason
            continue
        on_step(attempt=attempt, action='observe', route=route, reason='read_current_room')
        evidence = navigator.observe_live_room(deadline=deadline, stop_requested=stop_requested)
        last_status = compare_evidence(evidence, expected_room, anchor_uid)
        if last_status == IdentityStatus.UNCONFIRMED and not stop_requested() and time.monotonic() < deadline:
            # 先在已进入的直播页复检，避免因暂时缺失 UI 树不停重新导航。
            on_step(attempt=attempt, action='retry_observation', route=route, reason='wait_current_page')
            if navigator.wait_live_picture(deadline=min(deadline, time.monotonic()+6), stop_requested=stop_requested):
                evidence = navigator.observe_live_room(deadline=deadline, stop_requested=stop_requested)
                last_status = compare_evidence(evidence, expected_room, anchor_uid)
        last_evidence = evidence
        if last_status == IdentityStatus.MISMATCH:
            last_reason = 'id_mismatch'
            on_step(attempt=attempt, action='correct_wrong_room', route=route,
                    reason=last_reason, evidence=evidence)
            continue
        if last_status == IdentityStatus.UNCONFIRMED and not allow_unconfirmed:
            last_reason = 'room_evidence_unavailable'
            on_step(attempt=attempt, action='retry', route=route, reason=last_reason)
            continue
        if stop_requested():
            return LandingResult(False, 'cancelled', attempts=attempt)
        on_step(attempt=attempt, action='wait_picture', route=route, reason='return_to_live')
        if navigator.wait_live_picture(deadline=min(deadline, time.monotonic()+16), stop_requested=stop_requested):
            return LandingResult(True, 'target_located', last_status, evidence, attempt)
        last_reason = navigator.last_gate_reason
    return LandingResult(False, last_reason, last_status, last_evidence, attempted)
