"""连续采集的主线程生命周期：封装轮转不停止采集，校验后台执行。

仅故障、导航纠偏或任务停止结束采集会话；每个物理分段独立有唯一终态。
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
from pathlib import Path
import time

from src.worker.continuous_pipeline import ContinuousPipeline
from src.worker.events import (BaseWorkerEvent, SegmentStartedEvent, SegmentFinishedEvent,
    SegmentAbortedEvent, SegmentQualityWarningEvent, HeartbeatEvent, StallDetectedEvent, StallHealedEvent)
from src.worker.segment_verifier import finalize_segment
from src.worker.state_machine import WorkerState, ActionType
from src.worker.watchdog import StallWatchdog

log=logging.getLogger(__name__)


@dataclass
class CaptureSessionEvent(BaseWorkerEvent):
    event: str='capture_session'
    action: str=''
    capture_pid: int=0
    muxer_pid: int=0
    generation_id: int=0
    reason: str=''


@dataclass
class OverlayRecoveryEvent(BaseWorkerEvent):
    event: str='overlay_recovery'
    action: str=''
    overlay_kind: str=''
    success: bool=False
    suspected_start_time: float=0.0


class ContinuousSession:
    def __init__(self, controller, serial, scrcpy_exe, adb_exe,
                 pipeline_factory=ContinuousPipeline, watchdog_factory=StallWatchdog,
                 clock=time.monotonic, sleeper=time.sleep):
        self.c=controller
        self.serial,self.scrcpy,self.adb=serial,scrcpy_exe,adb_exe
        self.pipeline_factory,self.watchdog_factory=pipeline_factory,watchdog_factory
        self.clock,self.sleep=clock,sleeper
        self.pipe=None
        self.ledger={}
        self.pending={}
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

    def start_epoch(self):
        c=self.c
        if c.stop_requested:raise InterruptedError('停止后禁止启动采集')
        self.epoch+=1
        self.epoch_ended=False
        c.current_generation_id+=1
        c.action_dispatcher.current_generation_id=c.current_generation_id
        start_index=max(self.ledger,default=0)+1
        self.pipe=self.pipeline_factory(serial=self.serial,output_dir=c.task_output_dir,
            scrcpy_exe=self.scrcpy,adb_exe=self.adb,segment_duration_sec=c.config.segment_duration_sec,
            start_index=start_index)
        c._pipeline=self.pipe
        self.pipe.wait_hook=c._pipeline_wait_tick
        self.pipe.start()
        c.state_machine.transition_to(WorkerState.RECORDING)
        c.emitter.emit(CaptureSessionEvent(action='started',capture_pid=self.pipe._proc.pid,
            muxer_pid=self.pipe._mux.pid,generation_id=c.current_generation_id))
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
        c=self.c
        fragments=self.pipe.fragments()
        latest=max((f.index for f in fragments),default=0)
        for f in fragments:
            if f.index not in self.ledger:
                self.ledger[f.index]={'path':f.path,'terminal':False,'submitted':False,
                    'epoch':self.epoch,'generation':c.current_generation_id,'wall':time.time()}
                c.emitter.emit(SegmentStartedEvent(segment_index=f.index,file_path=str(f.path),
                    target_duration_sec=c.config.segment_duration_sec))
            row=self.ledger[f.index]
            row['closed']=f.closed
            c.current_segment_index=max(c.current_segment_index,f.index)
            c._highest_segment_index=max(c._highest_segment_index,f.index)
            if f.index==latest:c.current_file_path=f.path
            if row['terminal'] or row['submitted']:continue
            if ending and (not f.closed or (interrupted and f.index==latest)):
                self.abort(f.index,interrupted or 'fragment_not_closed')
                continue
            if not f.closed or (f.index==latest and not ending):continue
            if len(self.pending)>=4:
                raise RuntimeError('segment_validation_backlog')
            row['submitted']=True
            row['final']=bool(final and f.index==latest)
            target=c.task_output_dir/f'{c.target_room_id}_{c.run_id}_{f.index:06d}.mp4'
            self.pending[f.index]=self.pool.submit(finalize_segment,f.path,target,
                allow_short_tail=ending,timeout=8.0)
        self.collect()

    def abort(self,index,reason):
        row=self.ledger[index]
        if row['terminal']:return
        self.c.emitter.emit(SegmentAbortedEvent(segment_index=index,file_path=str(row['path']),reason=reason))
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
                    duration_sec=result.duration,file_size_mb=result.size_mb,is_final=row.get('final',False)))
                row.update(terminal=True,path=path)
                self.c._last_segment_failed=False

    def end_epoch(self, final=False, interrupted=''):
        c=self.c
        if c._watchdog:c._watchdog.stop()
        if not self.pipe or self.epoch_ended:return
        ok=self.pipe.stop()
        reason=interrupted or ('' if ok else 'capture_or_muxer_exit_failed')
        self.sync(ending=True,final=final and not reason,interrupted=reason)
        c.emitter.emit(CaptureSessionEvent(action='stopped',capture_pid=self.pipe._proc.pid if self.pipe._proc else 0,
            muxer_pid=self.pipe._mux.pid if self.pipe._mux else 0,
            generation_id=c.current_generation_id,reason=reason))
        self.epoch_ended=True
        if reason:self.last_error=reason

    def recover(self,reason,relocate):
        c=self.c
        if c.stop_requested:return False
        if not c._consume_recovery_budget():return False
        c.state_machine.transition_to(WorkerState.RECOVERING)
        c.emitter.emit(StallDetectedEvent(action='relocate_target' if relocate else 'restart_capture'))
        self.end_epoch(interrupted=reason)
        if relocate:
            landing=c._locate_target_room()
            if not landing.success:
                c._failure_reason=landing.reason;c._fatal_error=True;c.request_stop();return False
        if c.stop_requested:return False
        self.bad_pages=0
        self.start_epoch()
        c._failure_reason=''
        c.emitter.emit(StallHealedEvent(action='relocate_target' if relocate else 'restart_capture'))
        return True

    def poll_surface(self):
        c=self.c
        now=self.clock()
        if self.surface_future is not None and self.surface_future.done():
            try:observation=self.surface_future.result()
            except Exception:observation={'activity':'','ui_available':False}
            self.surface_future=None
            if self.surface_gen!=c.current_generation_id:return
            activity=observation.get('activity','')
            if 'LivePlayActivity' not in activity:
                self.bad_pages+=1
                if self.bad_pages>=2:self.recover('runtime_left_live_page',True)
            else:
                self.bad_pages=0
                kind=observation.get('overlay')
                if kind:
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
                    recovery_deadline=time.monotonic()+30
                    ok=c._navigator.dismiss_live_overlay(min(recovery_deadline,time.monotonic()+18),
                        lambda:c.stop_requested,initial_observation=observation,require_observed_clear=True)
                    if ok and not c.stop_requested:
                        # 上一步已确认面板消失，画面检查不重复再做一次慢UI dump。
                        ok=c._navigator.wait_live_picture(recovery_deadline,lambda:c.stop_requested,
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
                        if ok:
                            c.state_machine.transition_to(WorkerState.RECORDING)
                            c._watchdog=self.watchdog_factory(pipeline=self.pipe,navigator=c._navigator,
                                room_id=c.target_room_id,stall_threshold_sec=c.config.stall_timeout_sec,
                                generation_provider=lambda:c.current_generation_id,on_post_action=c.action_dispatcher.post_action)
                            c._watchdog.start();self.last_clear_wall=time.time()
                        else:self.recover('overlay_not_recovered',True)
                elif observation.get('ui_available'):
                    self.last_clear_wall=observation.get('observed_at',time.time())
            self.probe_failures=0 if activity else self.probe_failures+1
            if self.probe_failures>=3:self.recover('surface_probe_unavailable',True)
        if not c.stop_requested and self.surface_future is None and now-self.last_surface>=8:
            self.last_surface=now;self.surface_gen=c.current_generation_id
            self.surface_future=self.probes.submit(c._navigator.inspect_live_surface)

    def run(self):
        c=self.c
        try:
            self.start_epoch()
            self.started=self.clock()
            while not c.stop_requested:
                self.sync()
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
                self.poll_surface()
                if c.stop_requested:break
                now=self.clock()
                if now-self.last_heartbeat>=c.config.heartbeat_interval_sec:
                    snap=c._watchdog.snapshot() if c._watchdog else None
                    c.emitter.emit(HeartbeatEvent(elapsed_sec=int(now-self.started),
                        file_size_mb=round(self.pipe.get_current_file_size_mb(),2),status=c.state_machine.state.value.lower(),
                        room_identity_status=c.identity_status.value.lower(),fps=None,
                        video_motion=snap.video_motion if snap else None))
                    self.last_heartbeat=now
                if c.config.total_duration_sec and now-self.started>=c.config.total_duration_sec:break
                self.sleep(.1)
        finally:
            self.close()
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
            self.probes.shutdown(wait=True,cancel_futures=True)
            self.pool.shutdown(wait=True,cancel_futures=True)
            # 连续路径已收尾，旧串行清理器不能再对当前片发第二个终态。
            c.current_segment_started=False;c.current_segment_finalized=True
            self.closed=True
