"""运行期下播多源综合判定与动效容错滤波 (Ticket 11)。"""
import logging
from typing import Optional

logger = logging.getLogger("StreamEndDetector")


class StreamEndDetector:
    """下播信号多源交叉检验与时间滤波窗口。

    严禁单帧或瞬时关键字触发误下播，必须经时间窗口持续验证。
    """

    def __init__(self, confirm_window_sec: float = 5.0):
        self.confirm_window_sec = confirm_window_sec
        self._first_detected_time: Optional[float] = None

    @property
    def is_pending_confirmation(self) -> bool:
        return self._first_detected_time is not None

    def feed_observation(
        self,
        screen_shows_ended: bool,
        api_shows_ended: bool,
        current_time: float,
    ) -> tuple[bool, str]:
        """输入当前时刻屏幕特征与接口状态。

        返回 (is_confirmed_ended, reason)。
        """
        # 若屏幕或接口任一呈现下播迹象
        suspected = screen_shows_ended or api_shows_ended

        if not suspected:
            if self._first_detected_time is not None:
                logger.info("下播疑似信号已消失，画面恢复，重置确认窗口。")
            self._first_detected_time = None
            return False, "normal_live"

        # 首次发现下播迹象，开启确认窗口
        if self._first_detected_time is None:
            self._first_detected_time = current_time
            logger.info(f"捕获到下播疑似信号 (screen={screen_shows_ended}, api={api_shows_ended})，启动 {self.confirm_window_sec}s 滤波确认窗口...")
            return False, "pending_confirmation"

        # 检查是否持续超过滤波窗口
        duration = current_time - self._first_detected_time
        if duration >= self.confirm_window_sec:
            logger.info(f"下播状态已连续维持 {duration:.1f}s >= {self.confirm_window_sec}s，确诊主播下播！")
            return True, "stream_ended_confirmed"

        return False, "pending_confirmation"
