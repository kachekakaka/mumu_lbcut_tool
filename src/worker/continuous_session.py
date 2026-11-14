"""连续采集的主线程生命周期：封装轮转不停止采集，校验后台执行。

仅故障、导航纠偏或任务停止结束采集会话；每个物理分段独立有唯一终态。
"""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import logging
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
        for attempt in range(1,4):
            self.startup_attempt=attempt
            self.startup_reason=''
            self.epoch_accepted=False
            self.epoch_ended=True
            self.pipe=None
            try:
                self._check_startup_surface('before_capture')
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
            observation=c._navigator.inspect_live_surface(timeout_sec=1.0)
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
        self.epoch_ended=False
        self.quality_marks={}
        self.low_frame_rate=False
        self.frame_rate_ok_since=None
        self.on_live_page=False
        c.current_generation_id+=1
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
        self.epoch_accepted=True
        c.state_machine.transition_to(WorkerState.RECORDING)
        c.emitter.emit(CaptureSessionEvent(action='started',capture_pid=self.pipe._proc.pid,
            muxer_pid=self.pipe._mux.pid,generation_id=c.current_generation_id,
            capture_launch_at=getattr(self.pipe,'capture_launch_at',0.0),
            first_media_observed_at=getattr(self.pipe,'first_media_observed_at',0.0),
            surface_verified_at=self.surface_verified_at))
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
                ffprobe_exe=self.media_tools.ffprobe,allow_short_tail=ending,timeout=8.0)
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
        self.pipe.wait_hook=c._pipeline_wait_tick
        ok=self.pipe.stop()
        if not self.epoch_accepted:
            self.epoch_ended=True
            c.emitter.emit(CaptureStartupEvent(action='discarded',phase='cleanup',attempt=self.startup_attempt,
                reason=self.startup_reason or c._failure_reason or ('cancelled' if c.stop_requested else 'startup_failed'),
                directory=str(self.pipe.directory),
                capture_launch_at=getattr(self.pipe,'capture_launch_at',0.0),
                first_media_observed_at=getattr(self.pipe,'first_media_observed_at',0.0)))
            if not ok:raise RuntimeError('准备阶段采集收尾失败: '+self.pipe.failure_details())
            return
        self.media_done+=self.pipe.media_time() or 0.0
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
        # 设备离线时重新定位或重开采集都无意义；以 20 退出，由调度器同步实例在线状态后重开。
        if not self.device_online():
            self.device_lost();return False
        if not c._consume_recovery_budget():return False
        c.state_machine.transition_to(WorkerState.RECOVERING)
        action='relocate_target' if relocate else 'restart_capture'
        c.emitter.emit(StallDetectedEvent(action=action,reason=reason))
        self.end_epoch(interrupted=reason)
        if c.stop_requested:return False
        remaining=self.remaining_duration()
        if remaining is not None and remaining<1:
            self.limit_reached=True
            return False
        if relocate:
            landing=c._locate_target_room()
            if c.stop_requested:return False
            if not landing.success:
                if not self.device_online():
                    self.device_lost();return False
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
        current=[idx for idx,row in self.ledger.items() if row['epoch']==self.epoch]
        if not current:return
        latest=max(current)
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
            self.on_live_page='LivePlayActivity' in activity
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
                        lambda:c.stop_requested,require_observed_clear=True)
                    if ok and not c.stop_requested:
                        # 上一步已确认面板消失，画面检查不再重复遮挡检查。
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
                    break
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
            self.probes.shutdown(wait=True,cancel_futures=True)
            self.pool.shutdown(wait=True,cancel_futures=True)
            self.closed=True
