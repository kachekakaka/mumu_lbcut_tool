"""四维解耦录制健康检测与多信号综合评估器 (Ticket 10)。"""
from dataclasses import dataclass
import logging
from typing import Optional

logger = logging.getLogger("HealthMonitor")


@dataclass
class HealthSignals:
    """四维解耦录制健康特征快照。"""
    video_motion: float                    # 视频动态变化率 (0.0 ~ 1.0)
    audio_active: Optional[bool]           # 音频是否有活动 (True/False, None 表示不可获取/未知)
    process_alive: bool                    # scrcpy 录制子进程是否存活
    file_progress_mb: float                # 本周期文件增长大小 (MB)
    video_frozen_duration_sec: float = 0.0 # 视频独立冻结持续时长 (秒)
    file_stall_duration_sec: float = 0.0   # 文件停止增长持续时长 (秒)
    is_healthy: bool = True                # 综合健康判定
    alert_reason: str = ""                 # 异常告警原因


class HealthMonitor:
    """录制健康信号独立采集与联合裁决器。

    彻底废除“视频低且音频无活动才判异常”的错误假设。
    将视频动态、音频活动、进程存活和文件进度作为独立信号分别评估。
    视频冻结持续时间与文件停止增长持续时间分别计时，杜绝共用单一状态。
    """

    def __init__(
        self,
        video_freeze_threshold_sec: float = 15.0,
        file_stall_threshold_sec: float = 15.0,
    ):
        self.video_freeze_threshold_sec = video_freeze_threshold_sec
        self.file_stall_threshold_sec = file_stall_threshold_sec

    def evaluate_signals(
        self,
        video_motion: float,
        audio_active: Optional[bool],
        process_alive: bool,
        file_delta_mb: float,
        video_frozen_duration_sec: float = 0.0,
        file_stall_duration_sec: float = 0.0,
        # 兼容老签名
        frozen_duration_sec: float | None = None,
    ) -> HealthSignals:
        """根据四维解耦信号给出综合裁决。"""
        v_frozen_dur = frozen_duration_sec if frozen_duration_sec is not None else video_frozen_duration_sec
        f_stall_dur = file_stall_duration_sec

        # 1. 进程存活检查
        if not process_alive:
            return HealthSignals(
                video_motion=video_motion,
                audio_active=audio_active,
                process_alive=False,
                file_progress_mb=file_delta_mb,
                video_frozen_duration_sec=v_frozen_dur,
                file_stall_duration_sec=f_stall_dur,
                is_healthy=False,
                alert_reason="process_exited",
            )

        # 2. 视频画面冻结独立检查 (音频正常绝不能否决持续的视频冻结)
        if v_frozen_dur >= self.video_freeze_threshold_sec:
            logger.warning(
                f"视频画面持续冻结 {v_frozen_dur:.1f}s >= {self.video_freeze_threshold_sec}s！"
                f"(无论音频状态 audio_active={audio_active}，均独立判定画面冻结异常)"
            )
            return HealthSignals(
                video_motion=video_motion,
                audio_active=audio_active,
                process_alive=True,
                file_progress_mb=file_delta_mb,
                video_frozen_duration_sec=v_frozen_dur,
                file_stall_duration_sec=f_stall_dur,
                is_healthy=False,
                alert_reason="video_frozen",
            )

        # 3. 文件增长独立检查 (文件停止增长持续时间独立判定)
        if file_delta_mb <= 0.0 and f_stall_dur >= self.file_stall_threshold_sec:
            logger.warning(
                f"文件写入持续停滞 {f_stall_dur:.1f}s >= {self.file_stall_threshold_sec}s！"
            )
            return HealthSignals(
                video_motion=video_motion,
                audio_active=audio_active,
                process_alive=True,
                file_progress_mb=file_delta_mb,
                video_frozen_duration_sec=v_frozen_dur,
                file_stall_duration_sec=f_stall_dur,
                is_healthy=False,
                alert_reason="file_write_stalled",
            )

        return HealthSignals(
            video_motion=video_motion,
            audio_active=audio_active,
            process_alive=True,
            file_progress_mb=file_delta_mb,
            video_frozen_duration_sec=v_frozen_dur,
            file_stall_duration_sec=f_stall_dur,
            is_healthy=True,
            alert_reason="",
        )
