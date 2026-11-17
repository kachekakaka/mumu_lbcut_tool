"""运行期身份判定；时钟、采样和媒体生命周期由会话驱动。"""
from dataclasses import dataclass
from math import inf

from src.worker.identity_verifier import IdentityStatus
from src.worker.room_locator import RoomEvidence, compare_evidence

CHECK_INTERVAL = 10.0
OBSERVATION_BUDGET = 5.0
SAMPLE_INTERVAL = 1.0
CONFLICT_GRACE = 5.0
MAX_AGE = 30.0


@dataclass(frozen=True)
class IdentityObservation:
    evidence: RoomEvidence | None
    reason: str
    started: float
    finished: float
    wall_started: float
    wall_finished: float | None = None


class RuntimeIdentity:
    """身份状态跨采集代际保留；失证不刷新确认时间或冲突期限。"""
    def __init__(self, room_id, uid, allow_unconfirmed=False):
        self.room_id, self.uid = room_id, uid
        self.allow_unconfirmed = allow_unconfirmed
        self.confirmed_mono = self.confirmed_at = None
        self.checked_at = None
        self.source = 'unavailable'
        self.evidence_at = 0.0
        self.status = IdentityStatus.UNCONFIRMED
        self.reason = 'room_evidence_unavailable'
        self.issue_since = self.conflict_since = None
        self.matches = self.conflicts = 0
        self.conflict_key = None
        self.next_at = 0.0
        self.round_started = None
        self.pending_reason = ''

    def adopt(self, landing):
        self.status = landing.status
        self.matches = self.conflicts = 0
        self.round_started = None
        self.next_at = 0.0
        if landing.success and landing.status == IdentityStatus.VERIFIED:
            self.confirmed_mono, self.confirmed_at = landing.confirmed_mono, landing.confirmed_at
            self.source = landing.evidence.source if landing.evidence else 'unavailable'
            self.evidence_at = landing.evidence.observed_at if landing.evidence else 0.0
            self.issue_since = self.conflict_since = None
            self.reason = self.pending_reason = ''

    def age(self, now):
        return None if self.confirmed_mono is None else max(0.0, now-self.confirmed_mono)

    def stale(self, now):
        age = self.age(now)
        return age is None or age >= MAX_AGE

    def deadline(self):
        if self.allow_unconfirmed:
            return inf if self.conflict_since is None else self.conflict_since+MAX_AGE
        return min(self.confirmed_mono+MAX_AGE if self.confirmed_mono is not None else inf,
                   self.issue_since+MAX_AGE if self.issue_since is not None else inf)

    def suspect(self, now, reason):
        if self.issue_since is None:
            self.issue_since = now
        self.status = IdentityStatus.UNCONFIRMED
        self.reason = 'identity_conflict_unresolved' if self.conflict_since is not None else reason

    def recovery_reason(self, now):
        if self.pending_reason:
            return self.pending_reason
        if now >= self.deadline():
            return 'runtime_identity_conflict_unresolved' if self.conflict_since is not None else 'runtime_identity_unavailable'
        return ''

    def invalidate_samples(self):
        self.matches = self.conflicts = 0
        self.conflict_key = None
        self.round_started = None
        self.next_at = 0.0

    def accept(self, observation):
        """返回 confirmed / recovered / suspect；恢复只清当前身份，不清媒体风险。"""
        o = observation
        if self.round_started is None:
            self.round_started = o.started
        self.checked_at = o.wall_finished if o.wall_finished is not None else o.wall_started+o.finished-o.started
        candidate = compare_evidence(o.evidence, self.room_id, self.uid) if o.evidence else IdentityStatus.UNCONFIRMED
        self.next_at = o.finished+SAMPLE_INTERVAL
        if candidate == IdentityStatus.VERIFIED:
            self.matches += 1
            self.conflicts = 0
            self.conflict_key = None
            if self.matches >= 2:
                had_issue = self.issue_since is not None
                self.status = IdentityStatus.VERIFIED
                self.confirmed_mono, self.confirmed_at = o.started, o.wall_started
                self.source, self.evidence_at = o.evidence.source, o.evidence.observed_at
                self.issue_since = self.conflict_since = None
                self.reason = self.pending_reason = ''
                self.next_at = max(o.finished, self.round_started+CHECK_INTERVAL)
                self.round_started = None
                self.matches = 0
                return 'recovered' if had_issue else 'confirmed'
            return 'sample'
        self.matches = 0
        if candidate == IdentityStatus.MISMATCH:
            if self.conflict_since is None:
                self.conflict_since = o.finished
            key = (o.evidence.room_id, o.evidence.anchor_uid)
            if o.started >= self.conflict_since+CONFLICT_GRACE:
                self.conflicts = self.conflicts+1 if key == self.conflict_key else 1
            else:
                self.conflicts = 0
            self.conflict_key = key
            self.suspect(o.finished, 'identity_conflict_unresolved')
            if self.conflicts >= 2:
                self.status = IdentityStatus.MISMATCH
                self.reason = self.pending_reason = 'runtime_identity_mismatch'
        else:
            self.conflicts, self.conflict_key = 0, None
            self.suspect(o.finished, o.reason or 'room_evidence_unavailable')
            if self.allow_unconfirmed and self.conflict_since is None and o.finished-self.issue_since >= MAX_AGE:
                self.next_at = o.finished+CHECK_INTERVAL
                self.round_started = None
        return 'suspect'

    def context(self, now):
        return dict(room_identity_status=self.status.value.lower(), room_evidence_source=self.source,
                    room_evidence_at=self.evidence_at, room_identity_checked_at=self.checked_at,
                    room_identity_confirmed_at=self.confirmed_at, room_identity_age_sec=self.age(now),
                    room_identity_stale=self.stale(now), room_identity_reason=self.reason or None)
