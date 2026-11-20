"""连续采集的主线程生命周期：封装轮转不停止采集，校验后台执行。

仅故障、导航纠偏或任务停止结束采集会话；每个物理分段独立有唯一终态。
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
import math
import os
from pathlib import Path
import subprocess
import time

from src.worker.continuous_pipeline import ContinuousPipeline
from src.worker.events import (BaseWorkerEvent, SegmentStartedEvent, SegmentFinishedEvent,
    SegmentAbortedEvent, SegmentQualityWarningEvent, HeartbeatEvent, StallDetectedEvent, StallHealedEvent)
from src.worker.segment_verifier import finalize_segment
from src.worker.state_machine import WorkerState, ActionType
from src.worker.watchdog import StallWatchdog
from src.worker.runtime_identity import RuntimeIdentity, OBSERVATION_BUDGET, SAMPLE_INTERVAL, MAX_AGE
from src.worker.identity_verifier import IdentityStatus
from src.worker.events import RoomIdentityCheckEvent

log=logging.getLogger(__name__)

STATIC_MARK_SEC=10.0
STATIC_RELOCATE_SEC=120.0
EXIT_DEVICE_UNAVAILABLE=20


@dataclass
class CaptureSessionEvent(BaseWorkerEvent):
    event: str='capture_session'
    action: str=''
    capture_pid: int=0
    muxer_pid: int=0
    generation_id: int=0
    reason: str=''
    capture_launch_at: float=0.0
    first_media_observed_at: float=0.0
    surface_verified_at: float=0.0
    stop_decided_at: float=0.0
    capture_stopped_at: float=0.0
    stop_latency_sec: float=0.0


@dataclass
class CaptureStartupEvent(BaseWorkerEvent):
    event: str='capture_startup'
    action: str=''
    phase: str=''
    attempt: int=0
    reason: str=''
    focus_activity: str=''
    detail: str=''
    directory: str=''
    capture_launch_at: float=0.0
    first_media_observed_at: float=0.0


class StartupSurfaceChanged(RuntimeError):
    """准备阶段页面失效，必须停止临时采集后重新导航。"""


@dataclass
class StreamMonitorEvent(BaseWorkerEvent):
    event: str='stream_monitor'
    action: str=''
    reason: str=''
    analyzer_pid: int=0
    generation_id: int=0


@dataclass
class OverlayRecoveryEvent(BaseWorkerEvent):
    event: str='overlay_recovery'
    action: str=''
    overlay_kind: str=''
    success: bool=False
    suspected_start_time: float=0.0


class ContinuousSession:
    def __init__(self, controller, serial, scrcpy_exe, adb_exe, media_tools,
                 pipeline_factory=ContinuousPipeline, watchdog_factory=StallWatchdog,
                 clock=time.monotonic, sleeper=time.sleep, device_probe=None):
        self.c=controller
        self.serial=serial
        self.scrcpy=Path(scrcpy_exe).resolve() if scrcpy_exe else None
        self.adb=Path(adb_exe).resolve() if adb_exe else None
        self.media_tools=media_tools
        self.pipeline_factory,self.watchdog_factory=pipeline_factory,watchdog_factory
        self.clock,self.sleep=clock,sleeper
        self.device_online=device_probe or self._adb_device_online
        self.pipe=None
        self.ledger={}
        self.pending={}
        self.latest_segment_index=0
        self.held_fragment=None
        self.pool=ThreadPoolExecutor(max_workers=2,thread_name_prefix='segment-validation')
        self.probes=ThreadPoolExecutor(max_workers=1,thread_name_prefix='live-surface')
        self.surface_future=None
        self.surface_gen=None
        self.last_surface=0.0
        self.last_clear_wall=time.time()
        self.bad_pages=0
        self.probe_failures=0
        self.last_heartbeat=0.0
        self.epoch=0
        self.started=0.0
        self.last_error=''
        self.closed=False
        self.epoch_ended=False
        self.quality_marks={}
        self.low_frame_rate=False
        self.frame_rate_ok_since=None
        self.on_live_page=False
        self.static_relocated=False
        self.media_done=0.0
        self.limit_reached=False
        self.epoch_accepted=False
        self.startup_attempt=0
        self.startup_reason=''
        self.identity = getattr(controller, 'runtime_identity', None) or RuntimeIdentity(
            controller.target_room_id, getattr(controller, 'target_uid', ''),
            getattr(controller.config, 'allow_unconfirmed_identity', False))
        self.identity_probes=ThreadPoolExecutor(max_workers=1,thread_name_prefix='live-identity')
        self.identity_future=None
        self.identity_token=0
        self.identity_request=None
        self.identity_checkpoint=None
        self.identity_media_highwater=None
        self.identity_invalid_timeline=set()
        self.identity_risks={}
        self.identity_boundary_pending=False
        self.preparation_deadline=None

    def identity_context(self):
        context=self.identity.context(self.clock())
        self.c.identity_status=self.identity.status
        self.c.emitter.set_context(**context)
        return context

    def invalidate_identity_probe(self):
        self.identity_token+=1
        if self.identity_future:self.identity_future.cancel()
        # 未结束的请求保留到完成；不能取消失败后又堆入新请求。
        self.identity.invalidate_samples()

    def mark_identity_risk(self, mismatch=False):
        if not self.pipe or self.epoch_ended:return
        checkpoint=self.identity_checkpoint
        self.identity_media_position()
        lower=checkpoint[1] if checkpoint and checkpoint[0]==self.epoch and self.epoch not in self.identity_invalid_timeline else 0.0
        risk=self.identity_risks.setdefault(self.epoch,dict(
            lower=lower,wall=self.identity.confirmed_at or getattr(self.pipe,'capture_launch_at',time.time()),
            flags=set(),marked=set()))
        prior_flags = len(risk['flags'])
        risk['flags'].add('identity_unconfirmed')
        if mismatch:risk['flags'].add('identity_mismatch')
        if len(risk['flags']) != prior_flags or lower < risk['lower']:
            risk['lower'] = min(lower, risk['lower'])
            self.apply_identity_risks(epoch=self.epoch)

    def identity_media_position(self):
        """进度缺失、非有限值或回退时整代保守标记，不继续使用旧下界。"""
        if not self.pipe:return 0.0
        media=self.pipe.media_time()
        if media is None or not math.isfinite(media) or media<0:
            self.identity_invalid_timeline.add(self.epoch)
            return 0.0
        if self.identity_media_highwater is not None and media<self.identity_media_highwater:
            self.identity_invalid_timeline.add(self.epoch)
        self.identity_media_highwater=max(media,self.identity_media_highwater or 0.0)
        return media

    def apply_identity_risks(self, indices=None, epoch=None):
        if indices is None:
            entries = ((idx, row) for idx, row in self.ledger.items()
                       if epoch is None or row['epoch'] == epoch)
        else:
            entries = ((idx, self.ledger[idx]) for idx in indices)
        for index,row in entries:
            risk=self.identity_risks.get(row['epoch'])
            if not risk:continue
            if row['epoch'] in self.identity_invalid_timeline:risk['lower']=0.0
            end=row.get('end_pts')
            if end is not None and end < risk['lower']:continue
            row['identity_context']=dict(row.get('identity_context',{}),room_identity_status='unconfirmed')
            for flag in sorted(risk['flags']):
                key=(index,flag)
                if key in risk['marked']:continue
                risk['marked'].add(key)
                self.c.emitter.emit(SegmentQualityWarningEvent(segment_index=index,flag=flag,
                    suspected_start_time=risk['wall']),context=row['identity_context'])

    def emit_identity_observation(self, observation, action, accepted=True, request=None):
        evidence=observation.evidence
        self.c.emitter.emit(RoomIdentityCheckEvent(action=action,reason=observation.reason or self.identity.reason,
            generation_id=request[1] if request else self.c.current_generation_id,
            probe_id=request[0] if request else self.identity_token,
            observed_room_id=evidence.room_id if evidence else None,
            observed_anchor_uid=evidence.anchor_uid if evidence else None,
            sample_started_at=observation.wall_started,
            sample_finished_at=observation.wall_finished if observation.wall_finished is not None else observation.wall_started+observation.finished-observation.started,
            duration_sec=round(observation.finished-observation.started,3),accepted=accepted,
            anomaly_detected_at=time.time() if action=='suspect' else None))

    def accept_identity(self, observation, media):
        result=self.identity.accept(observation)
        if result=='suspect':self.mark_identity_risk(self.identity.status==IdentityStatus.MISMATCH)
        if result in ('confirmed','recovered'):
            self.identity_checkpoint=(self.epoch,media or 0.0)
            if self.epoch_accepted and not self.epoch_ended and self.epoch in self.identity_risks:
                self.identity_boundary_pending=True
        self.identity_context()
        if result!='sample':self.emit_identity_observation(observation,result)

    def check_identity_gate(self):
        """准备阶段过期复核，不发布正式片段，不消耗运行期恢复额度。"""
        c=self.c
        age=self.identity.age(self.clock())
        # 还需容纳运行期首轮双样本；不能在证据即将到期时刚好放行后立即恢复。
        if age is not None and age+2*OBSERVATION_BUDGET+SAMPLE_INTERVAL<MAX_AGE and self.identity.source=='live_fragment_arguments':return True
        if self.preparation_deadline is None:self.preparation_deadline=self.clock()+30
        uncertain=self.identity.stale(self.clock()) or self.identity.status!=IdentityStatus.VERIFIED or self.identity.source!='live_fragment_arguments'
        self.identity.invalidate_samples()
        self.identity.suspect(self.clock(),'startup_identity_recheck')
        # 采集启动已经写出的准备媒体也属于尚未复核的区间。
        if uncertain:self.mark_identity_risk()
        while self.clock()<self.preparation_deadline and not c.stop_requested:
            if not c._pipeline_wait_tick():break
            if self.clock()<self.identity.next_at:
                self.sleep(min(.1,self.identity.next_at-self.clock()));continue
            until=min(self.preparation_deadline,self.clock()+OBSERVATION_BUDGET)
            observation=c._navigator.inspect_live_identity(until,lambda:c.stop_requested)
            self.accept_identity(observation,self.identity_media_position())
            if self.identity.status==IdentityStatus.VERIFIED and not self.identity.stale(self.clock()):return True
            if observation.reason=='page_changed' or self.identity.pending_reason:
                raise StartupSurfaceChanged('startup_identity_changed')
        if c.stop_requested:return False
        if not self.device_online():
            self.device_lost();return False
        if self.identity.allow_unconfirmed and self.identity.conflict_since is None:
            self.identity_context();return True
        if self.identity.conflict_since is not None:
            c._failure_reason='runtime_identity_conflict_unresolved';c._exit_code=18
        else:
            c._failure_reason='runtime_identity_probe_unavailable';c._exit_code=9
        c._fatal_error=True;c.request_stop(c._failure_reason)
        return False

    def poll_identity(self, stopping=False):
        c=self.c
        if self.identity_future and self.identity_future.done():
            request=self.identity_request
            try:observation=self.identity_future.result()
            except Exception:observation=None
            self.identity_future=None
            if observation and request[0]==self.identity_token and request[1]==self.epoch and not c.stop_requested:
                self.accept_identity(observation,request[2])
            elif observation:self.emit_identity_observation(observation,'discarded',False,request)
        reason=self.identity.recovery_reason(self.clock())
        if reason:
            if reason!='runtime_identity_mismatch':self.identity.suspect(self.clock(),reason)
            self.mark_identity_risk(reason=='runtime_identity_mismatch')
        self.identity_context()
        if stopping or c.stop_requested:return False
        if reason:
            self.recover(reason,True);return True
        if self.identity_boundary_pending:
            self.recover('identity_risk_boundary_reset',False);return True
        if self.identity_future is None and self.clock()>=self.identity.next_at:
            self.identity_token+=1
            token=self.identity_token
            self.identity_request=(token,self.epoch,self.identity_media_position())
            deadline=min(self.clock()+OBSERVATION_BUDGET,self.identity.deadline())
            self.identity_future=self.identity_probes.submit(c._navigator.inspect_live_identity,
                deadline,lambda:c.stop_requested or token!=self.identity_token)
        return False

    def _adb_device_online(self):
        try:
            cwd = self.adb.parent if self.adb else None
            result=subprocess.run([str(self.adb),'-s',self.serial,'get-state'],capture_output=True,text=True,
                timeout=5,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0,
                cwd=cwd)
        except (OSError,subprocess.SubprocessError):
            return False
        return result.returncode==0 and result.stdout.strip()=='device'

    def device_lost(self):
        c=self.c
        c._failure_reason='device_offline'
        c._exit_code=EXIT_DEVICE_UNAVAILABLE
        c._fatal_error=True
        c.request_stop()

    def remaining_duration(self):
        """总时长按已录媒体时长扣减；未设总时长时返回 None。"""
        total=self.c.config.total_duration_sec
        return max(0.0,total-self.media_done) if total else None

    def start_epoch(self):
        """启动期间持续复核页面；最多三轮准备，不消耗运行期恢复次数。"""
        c=self.c
        self.preparation_deadline=None
        for attempt in range(1,4):
            self.startup_attempt=attempt
            self.startup_reason=''
            self.epoch_accepted=False
            self.epoch_ended=True
            self.pipe=None
            try:
                self._check_startup_surface('before_capture')
                if not self.check_identity_gate():return False
                self._start_prepared_epoch()
                return True
            except StartupSurfaceChanged as exc:
                self.startup_reason=str(exc)
                self.end_epoch()
                if c.stop_requested:return False
                if not self.device_online():
                    self.device_lost();return False
                if attempt==3:
                    c._failure_reason='startup_gate_exhausted: '+str(exc)
                    c._fatal_error=True
                    c.request_stop()
                    return False
                if self.startup_reason=='startup_identity_risk_boundary_reset':
                    # 身份已重新确认；旧准备媒体不发布，也不消费运行期恢复额度。
                    continue
                landing=c._locate_target_room()
                if c.stop_requested:return False
                if not landing.success:
                    c._exit_code=c._landing_exit_code(landing.reason)
                    c._fatal_error=True
                    c.request_stop()
                    return False
        return False

    def _check_startup_surface(self, phase):
        c=self.c
        if not c._pipeline_wait_tick():
            raise InterruptedError('开录准备期间收到停止请求')
        detail=''
        try:
            observation=c._navigator.inspect_live_surface(timeout_sec=2.0)
        except Exception as exc:
            observation={}
            detail=str(exc)
        activity=observation.get('activity','') or ''
        if 'login' in activity.lower():reason='login_prompt_detected'
        elif not activity or not observation.get('ui_available'):reason='surface_probe_unavailable'
        elif 'LivePlayActivity' not in activity:reason='startup_left_live_page'
        elif observation.get('overlay'):reason='startup_overlay_detected'
        else:reason=''
        self.surface_verified_at=time.time()
        c.emitter.emit(CaptureStartupEvent(action='rejected' if reason else 'checked',phase=phase,
            attempt=self.startup_attempt,reason=reason,focus_activity=activity,detail=detail))
        if c.stop_requested:raise InterruptedError('开录准备期间收到停止请求')
        if reason:
            log.warning('开录准备门禁拒绝: phase=%s attempt=%s activity=%s reason=%s detail=%s',
                        phase,self.startup_attempt,activity,reason,detail)
            raise StartupSurfaceChanged(reason)

    def _startup_wait_tick(self):
        try:
            if not self.c._pipeline_wait_tick():return False
            if self.clock()>=self.next_startup_probe:
                self._check_startup_surface('capture_starting')
                self.next_startup_probe=self.clock()+.5
            return True
        except BaseException:
            # Pipeline.start 的异常收尾也调用 wait_hook；禁止收尾中重复探针抛错。
            self.pipe.wait_hook=self.c._pipeline_wait_tick
            raise

    def _start_prepared_epoch(self):
        c=self.c
        if c.stop_requested:raise InterruptedError('停止后禁止启动采集')
        self.epoch+=1
        self.latest_segment_index=0
        self.held_fragment=None
        self.identity_media_highwater=None
        self.epoch_ended=False
        self.quality_marks={}
        self.low_frame_rate=False
        self.frame_rate_ok_since=None
        self.on_live_page=False
        c.current_generation_id+=1
        self.bad_pages=0
        self.probe_failures=0
        c.action_dispatcher.current_generation_id=c.current_generation_id
        start_index=max(self.ledger,default=0)+1
        self.pipe=self.pipeline_factory(serial=self.serial,output_dir=c.task_output_dir,
            scrcpy_exe=self.scrcpy,adb_exe=self.adb,ffmpeg_exe=self.media_tools.ffmpeg,
            segment_duration_sec=c.config.segment_duration_sec,
            start_index=start_index,total_duration_sec=self.remaining_duration() or 0)
        c._pipeline=self.pipe
        self.next_startup_probe=self.clock()
        self.pipe.wait_hook=self._startup_wait_tick
        self.pipe.start()
        self.pipe.wait_hook=c._pipeline_wait_tick
        self._check_startup_surface('before_recording')
        if not self.check_identity_gate():raise InterruptedError('身份准备门禁未通过')
        if self.identity.status==IdentityStatus.VERIFIED and self.epoch in self.identity_risks:
            raise StartupSurfaceChanged('startup_identity_risk_boundary_reset')
        self.epoch_accepted=True
        c.state_machine.transition_to(WorkerState.RECORDING)
        c.emitter.emit(CaptureSessionEvent(action='started',capture_pid=self.pipe._proc.pid,
            muxer_pid=self.pipe._mux.pid,generation_id=c.current_generation_id,
            capture_launch_at=getattr(self.pipe,'capture_launch_at',0.0),
            first_media_observed_at=getattr(self.pipe,'first_media_observed_at',0.0),
            surface_verified_at=self.surface_verified_at))
        if self.identity.issue_since is not None:self.mark_identity_risk()
        self.identity.next_at=0.0
        self.identity_context()
        self.sync()
        c._watchdog=self.watchdog_factory(pipeline=self.pipe,navigator=c._navigator,
            room_id=c.target_room_id,stall_threshold_sec=c.config.stall_timeout_sec,
            generation_provider=lambda:c.current_generation_id,
            on_post_action=c.action_dispatcher.post_action)
        c._watchdog.start()
        self.last_surface=self.clock()
        self.last_clear_wall=time.time()

    def sync(self, ending=False, final=False, interrupted=''):
        if not self.pipe:return
        was_invalid = self.epoch in self.identity_invalid_timeline
        self.identity_media_position()
        c=self.c
        fragments=self.pipe.fragment_updates(full=ending)
        if getattr(self.pipe, 'manifest_rebuilt', False):
            self.held_fragment = None
        latest=max((f.index for f in fragments),default=self.latest_segment_index)
        latest=self.latest_segment_index=max(self.latest_segment_index,latest)
        if self.held_fragment and (ending or self.held_fragment.index < latest):
            if not any(f.index == self.held_fragment.index for f in fragments):
                fragments.insert(0,self.held_fragment)
            self.held_fragment=None
        if getattr(self.pipe,'timeline_invalid',False):
            self.identity_invalid_timeline.add(self.epoch)
        for f in fragments:
            if f.index not in self.ledger:
                self.ledger[f.index]={'path':f.path,'terminal':False,'submitted':False,
                    'epoch':self.epoch,'generation':c.current_generation_id,'wall':time.time(),
                    'identity_context':self.identity.context(self.clock())}
                risk=self.identity_risks.get(self.epoch)
                if risk and (self.epoch in self.identity_invalid_timeline or f.end_pts is None or f.end_pts>=risk['lower']):
                    self.ledger[f.index]['identity_context']['room_identity_status']='unconfirmed'
                c.emitter.emit(SegmentStartedEvent(segment_index=f.index,file_path=str(f.path),
                    target_duration_sec=c.config.segment_duration_sec),context=self.ledger[f.index]['identity_context'])
            row=self.ledger[f.index]
            row['closed']=f.closed
            row['start_pts'],row['end_pts']=f.start_pts,f.end_pts
            if f.closed and (f.start_pts is None or f.end_pts is None or not math.isfinite(f.start_pts)
                             or not math.isfinite(f.end_pts) or f.start_pts<0 or f.end_pts<f.start_pts):
                self.identity_invalid_timeline.add(self.epoch)
            c.current_segment_index=max(c.current_segment_index,f.index)
            c._highest_segment_index=max(c._highest_segment_index,f.index)
            if f.index==latest:c.current_file_path=f.path
        risk=self.identity_risks.get(self.epoch)
        if risk:
            if self.epoch in self.identity_invalid_timeline and (not was_invalid or risk['lower']>0):
                self.apply_identity_risks(epoch=self.epoch)
            else:
                self.apply_identity_risks(f.index for f in fragments)
        for f in fragments:
            row=self.ledger[f.index]
            if row['terminal'] or row['submitted']:continue
            if ending and (not f.closed or (interrupted and f.index==latest)):
                self.abort(f.index,interrupted or 'fragment_not_closed')
                continue
            if not f.closed:continue
            if f.index==latest and not ending:
                self.held_fragment=f
                continue
            if len(self.pending)>=4:
                raise RuntimeError('segment_validation_backlog')
            row['submitted']=True
            row['final']=bool(final and f.index==latest)
            target=c.task_output_dir/f'{c.target_room_id}_{c.run_id}_{f.index:06d}.mp4'
            self.pending[f.index]=self.pool.submit(finalize_segment,f.path,target,
                ffprobe_exe=self.media_tools.ffprobe,allow_short_tail=ending,timeout=8.0)
        self.collect()

    def abort(self,index,reason):
        row=self.ledger[index]
        if row['terminal']:return
        self.c.emitter.emit(SegmentAbortedEvent(segment_index=index,file_path=str(row['path']),reason=reason),
                            context=row.get('identity_context'))
        row['terminal']=True
        self.c._had_segment_errors=True
        self.c._last_segment_failed=True
        self.last_error=reason

    def collect(self):
        # 结果按序发射；异步校验的是不可变旧片身份，不因新代际而丢弃。
        for index in sorted(self.pending):
            future=self.pending[index]
            if not future.done():break
            self.pending.pop(index)
            row=self.ledger[index]
            try:
                ok,path,result=future.result()
                if not ok:raise RuntimeError(result.reject_reason)
            except Exception as exc:
                self.abort(index,'validation_failed: '+str(exc))
            else:
                self.c.emitter.emit(SegmentFinishedEvent(segment_index=index,file_path=str(path),
                    duration_sec=result.duration,file_size_mb=result.size_mb,is_final=row.get('final',False)),
                    context=row.get('identity_context'))
                row.update(terminal=True,path=path)
                self.c._last_segment_failed=False

    def end_epoch(self, final=False, interrupted=''):
        c=self.c
        stop_started=self.clock()
        stop_decided_at=time.time()
        if c._watchdog:c._watchdog.stop()
        if not self.pipe or self.epoch_ended:return
        self.pipe.wait_hook=c._pipeline_wait_tick
        ok=self.pipe.stop(timeout_sec=max(.001,8-(self.clock()-stop_started)))
        if not self.epoch_accepted:
            self.epoch_ended=True
            c.emitter.emit(CaptureStartupEvent(action='discarded',phase='cleanup',attempt=self.startup_attempt,
                reason=self.startup_reason or c._failure_reason or ('cancelled' if c.stop_requested else 'startup_failed'),
                directory=str(self.pipe.directory),
                capture_launch_at=getattr(self.pipe,'capture_launch_at',0.0),
                first_media_observed_at=getattr(self.pipe,'first_media_observed_at',0.0)),
                context={'room_identity_status':'unconfirmed'} if self.epoch in self.identity_risks else None)
            if not ok:raise RuntimeError('准备阶段采集收尾失败: '+self.pipe.failure_details())
            return
        self.media_done+=self.pipe.media_time() or 0.0
        reason=interrupted or ('' if ok else 'capture_or_muxer_exit_failed')
        self.sync(ending=True,final=final and not reason,interrupted=reason)
        c.emitter.emit(CaptureSessionEvent(action='stopped',capture_pid=self.pipe._proc.pid if self.pipe._proc else 0,
            muxer_pid=self.pipe._mux.pid if self.pipe._mux else 0,
            generation_id=c.current_generation_id,reason=reason,stop_decided_at=stop_decided_at,
            capture_stopped_at=getattr(self.pipe,'capture_stopped_at',time.time()),
            stop_latency_sec=max(0,getattr(self.pipe,'capture_stopped_mono',self.clock())-stop_started)))
        self.epoch_ended=True
        if reason:self.last_error=reason
        if self.pipe.is_recording():raise RuntimeError('采集尚未停止，禁止启动下一代际')

    def recover(self,reason,relocate):
        c=self.c
        if c.stop_requested:return False
        if self.identity.conflict_since is not None or self.identity.recovery_reason(self.clock()):
            relocate=True
            reason=self.identity.recovery_reason(self.clock()) or 'runtime_identity_conflict_unresolved'
        self.invalidate_identity_probe()
        if reason.startswith('runtime_identity'):
            self.mark_identity_risk(reason=='runtime_identity_mismatch')
        # 身份到期先停止采集，不能被 ADB 在线查询延长；其他恢复保留离线优先判定。
        online_checked=not reason.startswith('runtime_identity')
        if online_checked and not self.device_online():
            self.device_lost();return False
        if not c._consume_recovery_budget():return False
        decided_at=time.time()
        c.state_machine.transition_to(WorkerState.RECOVERING)
        action='relocate_target' if relocate else 'restart_capture'
        c.emitter.emit(StallDetectedEvent(action=action,reason=reason))
        self.end_epoch(interrupted=reason)
        c.emitter.emit(RoomIdentityCheckEvent(action='capture_stopped',reason=reason,
            generation_id=c.current_generation_id,sample_started_at=decided_at,
            duration_sec=max(0,time.time()-decided_at),stop_decided_at=decided_at,
            capture_stopped_at=getattr(self.pipe,'capture_stopped_at',time.time())))
        self.identity_boundary_pending=False
        if c.stop_requested:return False
        if not online_checked and not self.device_online():
            self.device_lost();return False
        remaining=self.remaining_duration()
        if remaining is not None and remaining<1:
            self.limit_reached=True
            return False
        if relocate:
            self.identity_checkpoint=None
            landing=c._locate_target_room()
            if c.stop_requested:return False
            if not landing.success:
                if not self.device_online():
                    self.device_lost();return False
                c._exit_code=c._landing_exit_code(landing.reason)
                c._failure_reason=landing.reason;c._fatal_error=True;c.request_stop();return False
        self.bad_pages=0
        try:
            if not self.start_epoch():return False
        except InterruptedError:
            return False
        except Exception as exc:
            log.exception('重建采集失败')
            if not self.device_online():
                self.device_lost();return False
            c._failure_reason='capture_restart_failed: '+str(exc);c._fatal_error=True;c.request_stop()
            return False
        c._failure_reason=''
        c.emitter.emit(StallHealedEvent(action=action))
        c.emitter.emit(RoomIdentityCheckEvent(action='capture_recovered',reason=reason,
            generation_id=c.current_generation_id,recovered_at=time.time()))
        return True

    def check_stream_quality(self):
        """录像流分析的事件与切片质量标记；同一次持续异常对每个切片只标一次。"""
        monitor=getattr(self.pipe,'monitor',None)
        if monitor is None:return
        c=self.c
        for item in monitor.drain_events():
            c.emitter.emit(StreamMonitorEvent(generation_id=c.current_generation_id,**item))
        fps=monitor.fps(60)
        if fps is not None:
            now=monitor.clock()
            if fps<12:
                self.low_frame_rate=True
                self.frame_rate_ok_since=None
            elif self.low_frame_rate:
                # 帧率常在 12 上下波动；恢复须持续满一个统计窗口，否则每次越线都会重新标记。
                if self.frame_rate_ok_since is None:self.frame_rate_ok_since=now
                elif now-self.frame_rate_ok_since>=60:
                    self.low_frame_rate=False
                    self.frame_rate_ok_since=None
        latest=self.latest_segment_index
        if not latest:return
        static=self.picture_static_sec()
        for flag,active,lookback in (('visual_check_unavailable',monitor.unavailable,0),
                                     ('low_frame_rate',self.low_frame_rate,60),
                                     ('static_picture',static>=STATIC_MARK_SEC,static)):
            if not active:
                self.quality_marks.pop(flag,None)
                continue
            episode=self.quality_marks.setdefault(flag,{'since':time.time()-lookback,'marked':set()})
            if latest not in episode['marked']:
                episode['marked'].add(latest)
                c.emitter.emit(SegmentQualityWarningEvent(segment_index=latest,flag=flag,
                    suspected_start_time=episode['since']))

    def watchdog_snapshot(self):
        c=self.c
        dog=getattr(c,'_watchdog',None)
        snap=dog.snapshot() if dog else None
        if snap is None or snap.generation_id!=c.current_generation_id:return None
        return snap

    def picture_static_sec(self):
        snap=self.watchdog_snapshot()
        return snap.picture_static_sec if snap else 0.0

    def check_static_picture(self):
        """有帧但画面不变：满 2 分钟且确认仍在直播页时重新定位一次；画面恢复变化后才重新计。"""
        snap=self.watchdog_snapshot()
        if snap is None:return
        if snap.video_motion is not None and not snap.picture_static_sec and not snap.video_frozen_sec:
            self.static_relocated=False
        elif snap.picture_static_sec>=STATIC_RELOCATE_SEC and not self.static_relocated and self.on_live_page:
            self.static_relocated=True
            self.recover('static_picture_timeout',True)

    def heartbeat_fps(self):
        monitor=getattr(self.pipe,'monitor',None)
        fps=monitor.fps(10) if monitor else None
        return round(fps,1) if fps is not None else None

    def poll_surface(self):
        c=self.c
        now=self.clock()
        if self.surface_future is not None and self.surface_future.done():
            try:observation=self.surface_future.result()
            except Exception:observation={'activity':'','ui_available':False}
            self.surface_future=None
            if self.surface_gen!=c.current_generation_id:return
            activity=observation.get('activity','')
            if not activity or not observation.get('ui_available', True):
                self.bad_pages=0
                self.probe_failures+=1
                if self.probe_failures==3:self.recover('surface_probe_unavailable',True)
            elif 'LivePlayActivity' not in activity:
                self.on_live_page=False
                self.probe_failures=0
                self.bad_pages+=1
                if self.bad_pages==2:self.recover('runtime_left_live_page',True)
            else:
                self.on_live_page=True
                self.bad_pages=0
                self.probe_failures=0
                kind=observation.get('overlay')
                if kind:
                    self.invalidate_identity_probe()
                    self.sync()
                    affected=[idx for idx,row in self.ledger.items()
                              if row['epoch']==self.epoch and (not row['terminal'] or row['wall']>=self.last_clear_wall)]
                    for idx in affected:
                        c.emitter.emit(SegmentQualityWarningEvent(segment_index=idx,flag='overlay_'+kind,
                            suspected_start_time=self.last_clear_wall))
                    c.emitter.emit(OverlayRecoveryEvent(action='detected',overlay_kind=kind,
                        suspected_start_time=self.last_clear_wall))
                    c.state_machine.transition_to(WorkerState.RECOVERING)
                    if c._watchdog:c._watchdog.stop()
                    # 遮挡关闭不停止采集；持续保留原始媒体，风险区间通过事件说明。
                    recovery_deadline=min(time.monotonic()+30,self.identity.deadline())
                    def interrupted():
                        return c.stop_requested or self.clock()>=self.identity.deadline()
                    ok=c._navigator.dismiss_live_overlay(min(recovery_deadline,time.monotonic()+18),
                        interrupted,require_observed_clear=True)
                    if ok and not c.stop_requested:
                        # 上一步已确认面板消失，画面检查不再重复遮挡检查。
                        ok=c._navigator.wait_live_picture(recovery_deadline,interrupted,
                                                         check_overlay=False)
                    self.sync()
                    # 确认关闭期间采集仍在轮转；新发现的片段也必须保守标记，不能只标检测时那一片。
                    if affected:
                        for idx,row in self.ledger.items():
                            if row['epoch']==self.epoch and idx>=min(affected) and idx not in affected:
                                c.emitter.emit(SegmentQualityWarningEvent(segment_index=idx,flag='overlay_'+kind,
                                    suspected_start_time=self.last_clear_wall))
                    c.emitter.emit(OverlayRecoveryEvent(action='closed' if ok else 'failed',overlay_kind=kind,success=ok,
                        suspected_start_time=self.last_clear_wall))
                    if not c.stop_requested:
                        reason=self.identity.recovery_reason(self.clock())
                        if reason:
                            self.identity.suspect(self.clock(),reason)
                            self.mark_identity_risk();self.identity_context()
                            self.recover(reason,True)
                        elif ok:
                            c.state_machine.transition_to(WorkerState.RECORDING)
                            c._watchdog=self.watchdog_factory(pipeline=self.pipe,navigator=c._navigator,
                                room_id=c.target_room_id,stall_threshold_sec=c.config.stall_timeout_sec,
                                generation_provider=lambda:c.current_generation_id,on_post_action=c.action_dispatcher.post_action)
                            c._watchdog.start();self.last_clear_wall=time.time()
                        else:self.recover('overlay_not_recovered',True)
                elif observation.get('ui_available'):
                    self.last_clear_wall=observation.get('observed_at',time.time())
        # 窗口状态探针约 0.4 秒且不经渲染链路；连续 3 次读不到前台才重新定位，即约 30 秒。
        if not c.stop_requested and self.surface_future is None and now-self.last_surface>=10:
            self.last_surface=now;self.surface_gen=c.current_generation_id
            self.surface_future=self.probes.submit(c._navigator.inspect_live_surface)

    def run(self):
        c=self.c
        try:
            if not self.start_epoch():return 0 if c.stop_requested and not c._fatal_error else (c._exit_code or 30)
            self.started=self.clock()
            while not c.stop_requested and not self.limit_reached:
                self.sync()
                if self.pipe.reached_total_duration():
                    # 封装器已按总时长在录像时间轴上截止并封好最后一片。
                    self.limit_reached=True
                    self.poll_identity(stopping=True)
                    break
                # 先接收已经完成的身份证据，再仲裁采集故障，避免重启抹掉冲突。
                if self.poll_identity():continue
                if not self.pipe.is_recording():
                    log.error('持续采集异常: %s',self.pipe.failure_details())
                    if not self.recover('capture_process_exited',False):break
                    continue
                for action in c.action_dispatcher.drain_actions():
                    if c.stop_requested:break
                    if action.action_type==ActionType.FATAL_STOP:
                        # 遮挡/登录提示先尝试定位恢复，恢复预算耗尽才停止。
                        self.recover(action.reason,True)
                    elif action.action_type==ActionType.RESTART_SEGMENT:
                        self.recover(action.reason,action.reason!='file_progress_stalled')
                    else:self.recover(action.reason,True)
                    break
                if c.stop_requested:break
                self.check_stream_quality()
                self.poll_surface()
                if c.stop_requested or self.limit_reached:break
                self.check_static_picture()
                if c.stop_requested or self.limit_reached:break
                now=self.clock()
                if now-self.last_heartbeat>=c.config.heartbeat_interval_sec:
                    snap=c._watchdog.snapshot() if c._watchdog else None
                    c.emitter.emit(HeartbeatEvent(elapsed_sec=int(now-self.started),
                        segment_elapsed_sec=int(self.pipe.get_current_segment_elapsed()),
                        file_size_mb=round(self.pipe.get_current_file_size_mb(),2),status=c.state_machine.state.value.lower(),
                        room_identity_status=c.identity_status.value.lower(),fps=self.heartbeat_fps(),
                        video_motion=snap.video_motion if snap else None))
                    self.last_heartbeat=now
                self.sleep(.1)
        finally:
            self.poll_identity(stopping=True)
            self.close()
        if c._exit_code:
            return c._exit_code
        if c._fatal_error or c._last_segment_failed or not self.ledger:
            c._failure_reason=c._failure_reason or self.last_error or 'no_recorded_segment'
            return 30
        return 0

    def close(self):
        if self.closed:return
        c=self.c
        if c.state_machine.state!=WorkerState.STOPPED:c.state_machine.transition_to(WorkerState.STOPPING)
        try:
            self.end_epoch(final=True,interrupted=c._failure_reason if c._fatal_error else '')
            deadline=self.clock()+24
            while self.pending and self.clock()<deadline:
                self.collect()
                if self.pending:self.sleep(.05)
            for idx in list(self.pending):
                self.pending[idx].cancel()
                self.abort(idx,'validation_deadline_exceeded')
            self.pending.clear()
            for idx,row in self.ledger.items():
                if not row['terminal']:self.abort(idx,'fragment_terminal_missing')
        finally:
            self.invalidate_identity_probe()
            self.identity_probes.shutdown(wait=True,cancel_futures=True)
            self.probes.shutdown(wait=True,cancel_futures=True)
            self.pool.shutdown(wait=True,cancel_futures=True)
            self.closed=True
