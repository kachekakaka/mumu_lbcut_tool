"""Worker 单写者生命周期状态机与代际动作队列 (Ticket 05)。"""
from enum import Enum
from dataclasses import dataclass
import logging
import queue
import time
from typing import Optional

logger = logging.getLogger("WorkerStateMachine")


class WorkerState(str, Enum):
    """Worker 五阶段核心生命周期状态。"""
    PREFLIGHT = "PREFLIGHT"
    RECORDING = "RECORDING"
    RECOVERING = "RECOVERING"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"


class ActionType(str, Enum):
    """看门狗与后台检测投递给主线程的动作意图。"""
    IN_PLACE_HEAL = "IN_PLACE_HEAL"
    RESTART_SEGMENT = "RESTART_SEGMENT"
    FATAL_STOP = "FATAL_STOP"


@dataclass
class PipelineAction:
    """管线控制动作意图，必须携带快照代际 ID。"""
    action_type: ActionType
    generation_id: int
    reason: str
    created_at: float = 0.0

    def __post_init__(self):
        if not self.created_at:
            self.created_at = time.time()


class InvalidStateTransitionError(RuntimeError):
    """状态机非法迁移异常。"""
    pass


class WorkerStateMachine:
    """严格有向状态机。

    合法迁移图：
    - PREFLIGHT  -> RECORDING, STOPPING
    - RECORDING  -> RECOVERING, STOPPING
    - RECOVERING -> RECORDING, STOPPING
    - STOPPING   -> STOPPED
    - STOPPED    -> (终态，禁止任何迁出)
    重复请求当前状态为幂等空操作；所有结束路径必须先进入 STOPPING。
    """

    ALLOWED_TRANSITIONS = {
        WorkerState.PREFLIGHT: {WorkerState.RECORDING, WorkerState.STOPPING},
        WorkerState.RECORDING: {WorkerState.RECOVERING, WorkerState.STOPPING},
        WorkerState.RECOVERING: {WorkerState.RECORDING, WorkerState.STOPPING},
        WorkerState.STOPPING: {WorkerState.STOPPED},
        WorkerState.STOPPED: set(),  # 终态无迁出
    }

    def __init__(self, initial_state: WorkerState = WorkerState.PREFLIGHT):
        self._state = initial_state

    @property
    def state(self) -> WorkerState:
        return self._state

    def transition_to(self, new_state: WorkerState) -> None:
        """执行状态迁移，校验合法性。"""
        if new_state == self._state:
            return

        allowed = self.ALLOWED_TRANSITIONS.get(self._state, set())
        if new_state not in allowed:
            raise InvalidStateTransitionError(
                f"非法状态跃迁: 无法从 {self._state.value} 直接迁入 {new_state.value}。"
            )

        logger.info(f"Worker 状态机迁移: {self._state.value} -> {new_state.value}")
        self._state = new_state


class GenerationActionDispatcher:
    """单写者代际动作分发器。

    看门狗与后台任务仅投递动作请求；主控制线程统一消费并校验代际 ID。
    非当前代际的过期请求将被安全丢弃，彻底消除时序竞争与幽灵重启。
    """

    def __init__(self):
        self._queue: queue.Queue[PipelineAction] = queue.Queue()
        self.current_generation_id: int = 0

    def post_action(self, action: PipelineAction) -> None:
        """后台只读观察者向主线程投递动作意图。"""
        self._queue.put(action)

    def drain_actions(self) -> list[PipelineAction]:
        """主线程排空并过滤队列，仅返回代际匹配的有效动作，同代际同类动作去重合并。"""
        valid_actions: list[PipelineAction] = []
        seen_types: set[ActionType] = set()

        while not self._queue.empty():
            try:
                action = self._queue.get_nowait()
            except queue.Empty:
                break

            if action.generation_id == self.current_generation_id:
                if action.action_type not in seen_types:
                    seen_types.add(action.action_type)
                    valid_actions.append(action)
                else:
                    logger.debug(f"同代际重复动作去重忽略: action={action.action_type.value}")
            else:
                logger.warning(
                    f"丢弃过期代际动作: action={action.action_type.value}, "
                    f"action_gen={action.generation_id}, current_gen={self.current_generation_id}, reason={action.reason}"
                )
        priority = {ActionType.FATAL_STOP: 0, ActionType.RESTART_SEGMENT: 1, ActionType.IN_PLACE_HEAL: 2}
        return sorted(valid_actions, key=lambda action: priority[action.action_type])
