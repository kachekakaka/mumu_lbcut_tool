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


def current_live_fragment_evidence(activity_top: str, current_activity: str | None) -> RoomEvidence | None:
    """从当前 LivePlayActivity 的 LivePlayFragment 参数读取当前场次与主播 UID。

    只采信 live_backend_roomid + USER_ID；FROM_ROOM_ID 是跳转来源，不能作为当前房间。
    ROOM_ID / EXTRA_ROOM_UNIQUE_KEY 若存在，必须与当前 room_id 一致。多个 Fragment
    副本必须全部一致；冲突、缺 UID 或非直播 Activity 均回退到完整资料页取证。
    """
    if not current_activity or 'LivePlayActivity' not in current_activity:
        return None
    text = str(activity_top or '')
    # dumpsys embeds empty Bundle[{}] values inside mArguments; normalize them so
    # the outer Bundle terminator is not mistaken for the nested empty bundle.
    text = re.sub(r'Bundle\[\{\}\]', 'Bundle[empty]', text)
    bundles = re.findall(r'mArguments=Bundle\[\{(.*?)\}\]', text, re.DOTALL)
    candidates = []
    for body in bundles:
        if 'live_backend_roomid=' not in body or 'is_LiveRoomFragment=true' not in body:
            continue
        rooms = re.findall(r'live_backend_roomid=(\d+)', body)
        uids = re.findall(r'live\.intent\.extra\.USER_ID=(\d+)', body)
        room_args = re.findall(r'live\.intent\.extra\.ROOM_ID=(\d+)', body)
        unique = re.findall(r'live\.intent\.extra\.EXTRA_ROOM_UNIQUE_KEY=(\d+)_\d+', body)
        if len(set(rooms)) != 1 or len(set(uids)) != 1:
            return None
        room_id, uid = rooms[0], uids[0]
        if uid == '0' or not room_id.isdigit() or not uid.isdigit():
            return None
        if room_args and (len(set(room_args)) != 1 or room_args[0] != room_id):
            return None
        if unique and (len(set(unique)) != 1 or unique[0] != room_id):
            return None
        candidates.append((room_id, uid, unique[0] if unique else None))
    if not candidates or len({(r, u) for r, u, _ in candidates}) != 1:
        return None
    room_id, uid, _ = candidates[0]
    tokens = {token for _, _, token in candidates if token}
    token = next(iter(tokens)) if len(tokens) == 1 else None
    return RoomEvidence(room_id, uid, None, 'live_fragment_arguments', token, time.time())


def compare_evidence(evidence: RoomEvidence, room_id: str | None,
                     anchor_uid: str | None) -> IdentityStatus:
    """比较同类稳定标识；昵称只展示，不用同名文本替代身份匹配。"""
    pairs = [(room_id, evidence.room_id), (anchor_uid, evidence.anchor_uid)]
    comparable = [(str(a), str(b)) for a,b in pairs if a and b]
    if any(a != b for a,b in comparable):
        return IdentityStatus.MISMATCH
    return IdentityStatus.VERIFIED if comparable else IdentityStatus.UNCONFIRMED


DEFAULT_NAVIGATION_TIMEOUT_SEC = 300.0


def validate_navigation_timeout(value: float) -> None:
    """对外配置必须有限、有界，不能用 NaN/inf 关闭超时保护。"""
    import math
    if not math.isfinite(value) or not 60 <= value <= 900:
        raise ValueError('navigation_timeout_sec 必须在 60～900 秒之间')


def locate_target(navigator, target: str, *, target_kind: str = 'auto',
                  web_rid: str | None = None, anchor_uid: str | None = None,
                  timeout_sec: float = DEFAULT_NAVIGATION_TIMEOUT_SEC, max_attempts: int = 3,
                  stop_requested=lambda: False, on_step=lambda **kw: None,
                  allow_unconfirmed: bool = False) -> LandingResult:
    """定位阶段共享总截止时间，但为最终返回/画面确认保留预算。

    初始只确认直播 Activity；画面就绪在取得独立身份之后检查一次。
    同页取证允许有界复检；已核验后暂时读不到页面/画面只等待当前步骤，
    不重新打开资料页。明确离开直播页或身份不匹配才重新导航并作废旧证据。
    """
    import math
    if not math.isfinite(timeout_sec) or timeout_sec <= 0 or max_attempts < 1:
        raise ValueError('定位时限和尝试次数必须为有限正数')
    started = time.monotonic()
    deadline = started + timeout_sec
    reserve = min(45.0, timeout_sec / 3)
    preparation_deadline = deadline - reserve
    internal = target_kind == 'room_id' or (target_kind == 'auto' and bool(re.fullmatch(r'\d{19}', target)))
    expected_room = target if internal else None
    last_reason, last_status, last_evidence = 'navigation_failed', IdentityStatus.UNCONFIRMED, None
    attempted, phase, route = 0, 'navigation', ''

    def emit(action, reason='', evidence=None, stage_deadline=None):
        now = time.monotonic()
        on_step(attempt=attempted, action=action, route=route, phase=phase,
                reason=reason, evidence=evidence, elapsed_sec=round(now-started, 3),
                remaining_sec=round(max(0.0, deadline-now), 3),
                stage_budget_sec=round(max(0.0, (stage_deadline or deadline)-now), 3))

    def pause(seconds, until):
        end = min(until, time.monotonic()+seconds)
        while time.monotonic() < end:
            if stop_requested():
                return False
            time.sleep(min(.1, max(0.0, end-time.monotonic())))
        return not stop_requested()

    def finish(success, reason):
        if stop_requested():
            success, reason = False, 'cancelled'
        return LandingResult(success, reason, last_status, last_evidence, attempted)

    def device_lost():
        # 探测失败多数只是页面慢；只有 adb 确认实例掉线才提前结束，交给上层换实例，不耗尽整段预算。
        if stop_requested() or navigator.device_online():
            return False
        emit('device_lost', 'device_unavailable')
        return True

    for attempt in range(1, max_attempts+1):
        if stop_requested():
            return finish(False, 'cancelled')
        # 不把最后几秒分给另一次跳转；保留已知失败原因，不启动注定超时的命令。
        if preparation_deadline-time.monotonic() < 8:
            emit('budget_exhausted', last_reason)
            break
        attempted = attempt
        route = 'primary' if attempt == 1 else ('web' if attempt == 2 and web_rid else 'reset')
        if last_status == IdentityStatus.MISMATCH:
            route = 'reset'
        # 每一次实际重新导航都使上一页身份依据失效，禁止跨页面复用。
        last_status, last_evidence = IdentityStatus.UNCONFIRMED, None
        phase = 'navigation'
        until = min(preparation_deadline, time.monotonic()+30)
        # 首次导航尚无失败原因；重试时带上一次的失败原因便于排查。
        emit('navigate', last_reason if attempt > 1 else '', stage_deadline=until)
        if not navigator.navigate_route(target, web_rid, route, until, stop_requested):
            last_reason = 'intent_failed'
            if device_lost():
                return finish(False, 'device_unavailable')
            continue
        phase = 'live_page'
        until = min(preparation_deadline, time.monotonic()+45)
        emit('wait_live_page', 'wait_page_loading', stage_deadline=until)
        if not navigator.wait_live_page(deadline=until, stop_requested=stop_requested):
            last_reason = navigator.last_gate_reason or 'page_probe_unavailable'
            if last_reason == 'page_probe_unavailable' and device_lost():
                return finish(False, 'device_unavailable')
            emit('retry', last_reason)
            continue

        phase = 'identity'
        # 单次取证最多60秒，包含返回自己打开的资料页的清理；不是拿走所有总预算。
        for read_attempt in range(2):
            if stop_requested():
                return finish(False, 'cancelled')
            if preparation_deadline-time.monotonic() < 12:
                last_reason = 'room_evidence_unavailable'
                break
            until = min(preparation_deadline, time.monotonic()+60)
            emit('observe' if read_attempt == 0 else 'retry_observation',
                 'read_current_room' if read_attempt == 0 else 'wait_current_page', stage_deadline=until)
            last_evidence = navigator.observe_live_room(deadline=until, stop_requested=stop_requested)
            last_status = compare_evidence(last_evidence, expected_room, anchor_uid)
            if last_status != IdentityStatus.UNCONFIRMED:
                break
            if read_attempt == 0 and not pause(1.0, preparation_deadline):
                return finish(False, 'cancelled')
        if stop_requested():
            return finish(False, 'cancelled')
        if last_status == IdentityStatus.MISMATCH:
            last_reason = 'id_mismatch'
            emit('correct_wrong_room', last_reason, last_evidence)
            continue
        if last_status == IdentityStatus.UNCONFIRMED and not allow_unconfirmed:
            last_reason = 'room_evidence_unavailable'
            emit('retry', last_reason, last_evidence)
            continue

        phase = 'picture'
        # 至少预留一个完整确认窗口；慢加载在本阶段复检，不重新点击/取证。
        while deadline-time.monotonic() >= 5:
            if stop_requested():
                return finish(False, 'cancelled')
            until = min(deadline, time.monotonic()+45)
            emit('wait_picture', 'return_to_live', last_evidence, until)
            if navigator.wait_live_picture(deadline=until, stop_requested=stop_requested,
                                           return_from_profile=True):
                return finish(True, 'target_located')
            if stop_requested():
                return finish(False, 'cancelled')
            last_reason = navigator.last_gate_reason or 'page_probe_unavailable'
            if last_reason == 'page_probe_unavailable' and device_lost():
                return finish(False, 'device_unavailable')
            if last_reason in {'navigation_failed', 'login_blocked'}:
                emit('page_changed', last_reason)
                last_status, last_evidence = IdentityStatus.UNCONFIRMED, None
                break  # 已明确离开目标页面；下一次导航不沿用原来的身份依据。
            emit('retry_picture', last_reason, last_evidence)
            if not pause(1.0, deadline):
                return finish(False, 'cancelled')
        else:
            emit('budget_exhausted', last_reason, last_evidence)
            return finish(False, last_reason)
    return finish(False, last_reason)
