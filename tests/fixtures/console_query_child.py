"""查询执行器测试夹具：模拟网络阶段、部分 IPC 和正常解析器复用。"""
import json
import os
import sys
import time

count = 0
for line in sys.stdin.buffer:
    raw = json.loads(line)['raw']
    count += 1
    if raw.startswith('block_'):
        # 分别代表 DNS、游客锁、重试、响应读取或解析永不返回。
        if raw == 'block_partial':
            sys.stdout.buffer.write(b'{"room":')
            sys.stdout.buffer.flush()
        while True:
            time.sleep(10)
    if raw == 'oversize':
        sys.stdout.buffer.write(b'x' * 70000 + b'\n')
        sys.stdout.buffer.flush()
        continue
    if raw == 'exit':
        sys.exit(2)
    result = dict(room=dict(web_rid=raw, status='live', room_id=f'{os.getpid()}:{count}',
                           title=None, nickname='合成目标', anchor_uid=None), error=None)
    sys.stdout.buffer.write(json.dumps(result).encode('utf-8') + b'\n')
    sys.stdout.buffer.flush()
