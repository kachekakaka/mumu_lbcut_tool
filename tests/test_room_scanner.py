"""按目标收取结果；慢目标不阻塞其他观测，未派发不分配序号。"""
from console_helpers import DeferredQueries
from src.mvp.room_resolver import ResolvedRoom, RoomStatus
from src.orchestrator.room_query_process import QueryResult
from src.orchestrator.room_scanner import ObservationDispatcher
from src.orchestrator.storage import Store


def test_individual_results_and_no_sequence_for_deferred(tmp_path):
    store = Store(tmp_path / 'state.sqlite3')
    try:
        for raw in ['A', 'B', 'C']:
            store.add_target(raw)
        targets = store.rows('SELECT * FROM targets ORDER BY raw')
        executor = DeferredQueries()
        scanner = ObservationDispatcher(store, executor, clock=lambda: 10)
        assert scanner.dispatch('manual', targets[0])
        assert not scanner.dispatch('manual', targets[0])
        assert scanner.dispatch('manual', targets[1])
        assert not scanner.dispatch('manual', targets[2])
        assert store.row('SELECT latest_observation_seq FROM targets WHERE id=?', (targets[2]['id'],))['latest_observation_seq'] == 0
        executor.calls[1][2].set_result(QueryResult(ResolvedRoom('B', RoomStatus.LIVE)))
        results = list(scanner.collect())
        assert len(results) == 1 and results[0][1]['target_id'] == targets[1]['id']
        assert not executor.calls[0][2].done()
    finally:
        store.close()
