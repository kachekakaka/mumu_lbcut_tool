"""调度层统一观测派发；所有结果逐目标返回，不保留旧批次覆盖入口。"""
import time


class ObservationDispatcher:
    """所有来源共用派发序号；没有空槽位时不产生观测。"""
    def __init__(self, store, executor, clock=time.time):
        self.store, self.executor, self.clock = store, executor, clock
        self.pending = {}

    def dispatch(self, source, target, subject=None):
        key = (source, target["id"])
        if key in self.pending or not self.executor.available(source):
            return False
        meta = self.store.begin_observation(target["id"], self.clock())
        future = self.executor.submit(source, target["raw"])
        if future is None:
            raise RuntimeError("已确认的查询槽位在派发时丢失")
        self.pending[key] = (meta, subject, future)
        return True

    def collect(self):
        if self.executor.fatal_error:
            raise RuntimeError(self.executor.fatal_error)
        for key, (meta, subject, future) in list(self.pending.items()):
            if future.done():
                del self.pending[key]
                yield key[0], meta, subject, future.result()
