"""测试管线单写者架构、代际队列与严格状态机 (Ticket 05)。"""
from unittest.mock import MagicMock
import pytest

from src.worker.state_machine import (
    WorkerState,
    WorkerStateMachine,
    InvalidStateTransitionError,
    PipelineAction,
    ActionType,
)
from src.worker.events import (
    WorkerEventEmitter,
    SegmentStartedEvent,
    SegmentFinishedEvent,
    SegmentAbortedEvent,
)


def test_state_machine_valid_transitions():
    """状态机允许合法迁移：PREFLIGHT -> RECORDING -> RECOVERING -> RECORDING -> STOPPING -> STOPPED。"""
    sm = WorkerStateMachine()
    assert sm.state == WorkerState.PREFLIGHT

    sm.transition_to(WorkerState.RECORDING)
    assert sm.state == WorkerState.RECORDING

    sm.transition_to(WorkerState.RECOVERING)
    assert sm.state == WorkerState.RECOVERING

    sm.transition_to(WorkerState.RECORDING)
    assert sm.state == WorkerState.RECORDING

    sm.transition_to(WorkerState.STOPPING)
    assert sm.state == WorkerState.STOPPING

    sm.transition_to(WorkerState.STOPPED)
    assert sm.state == WorkerState.STOPPED


@pytest.mark.parametrize('source', list(WorkerState))
@pytest.mark.parametrize('target', list(WorkerState))
def test_state_machine_transition_contract(source, target):
    """独立按 ADR 0008 核对全部状态对，不从实现的允许表推导预期。"""
    permitted = {
        ('PREFLIGHT', 'RECORDING'), ('PREFLIGHT', 'STOPPING'),
        ('RECORDING', 'RECOVERING'), ('RECORDING', 'STOPPING'),
        ('RECOVERING', 'RECORDING'), ('RECOVERING', 'STOPPING'),
        ('STOPPING', 'STOPPED'),
    }
    sm = WorkerStateMachine(source)
    if source == target or (source.value, target.value) in permitted:
        sm.transition_to(target)
        assert sm.state == target
    else:
        with pytest.raises(InvalidStateTransitionError):
            sm.transition_to(target)
        assert sm.state == source


def test_generation_action_queue_discards_stale_actions():
    """主控制线程消费动作队列时，比对 generation_id；若不匹配当前代际则安全丢弃。"""
    from src.worker.state_machine import GenerationActionDispatcher

    dispatcher = GenerationActionDispatcher()
    # 当前代际为 1
    dispatcher.current_generation_id = 1

    processed_actions = []

    # 投递旧代际 0 的重启请求（属于上一段切片的残留动作）
    stale_action = PipelineAction(
        action_type=ActionType.RESTART_SEGMENT,
        generation_id=0,
        reason="stale_stall_watchdog",
    )
    dispatcher.post_action(stale_action)

    # 投递当前代际 1 的正常自愈动作
    valid_action = PipelineAction(
        action_type=ActionType.IN_PLACE_HEAL,
        generation_id=1,
        reason="current_intent_refresh",
    )
    dispatcher.post_action(valid_action)

    # 主线程消费队列
    consumed = dispatcher.drain_actions()
    assert len(consumed) == 1
    assert consumed[0].action_type == ActionType.IN_PLACE_HEAL
    assert consumed[0].generation_id == 1


def test_event_lifecycle_contract_segment_aborted_pairing():
    """事件完整性契约：每个 segment_started 必须有且仅有一个终态事件 (finished 或 aborted)。"""
    mock_stream = MagicMock()
    emitter = WorkerEventEmitter(stream=mock_stream)

    # 模拟启动切片
    emitter.emit(SegmentStartedEvent(segment_index=1, file_path="seg_1.mp4"))
    # 模拟异常中止
    emitter.emit(SegmentAbortedEvent(segment_index=1, file_path="seg_1.mp4", reason="process_killed"))

    calls = mock_stream.write.call_args_list
    assert len(calls) == 2
    assert "segment_started" in calls[0][0][0]
    assert "segment_aborted" in calls[1][0][0]
    assert "process_killed" in calls[1][0][0]
