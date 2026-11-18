from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from scripts._common import entry, input_file, preview

def read_samples(path):
    rows = json.loads(input_file(path).read_text(encoding='utf-8-sig'))
    if not isinstance(rows, list) or not rows:
        raise ValueError('样本必须为非空 JSON 数组')
    ids, entries = set(), set()
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('id'), int) or isinstance(row['id'], bool):
            raise ValueError('样本缺少整数 id')
        value = row.get('entry')
        if not isinstance(value, str) or not value.strip():
            raise ValueError('样本缺少 entry')
        value = value.strip()
        match = re.fullmatch(r'https?://live\.douyin\.com/([\w.~-]+)/?(?:\?[^\s]*)?', value)
        normalized = match.group(1) if match else value
        if not re.fullmatch(r'[a-zA-Z0-9._~-]+', normalized):
            raise ValueError('样本只接受 web_rid 或直播网页完整链接，不在输入检查时解析短链')
        if row['id'] in ids or normalized in entries:
            raise ValueError('样本 id 或规范化入口重复')
        ids.add(row['id']); entries.add(normalized)
        yield dict(row, entry=normalized)

def main(argv=None):
    p = argparse.ArgumentParser(description='显式查询样本开播状态，不操作设备')
    p.add_argument('--input', required=True)
    p.add_argument('--workers', type=int, choices=range(1, 11), default=2)
    p.add_argument('--execute', action='store_true')
    args = p.parse_args(argv)
    rows = list(read_samples(args.input))
    if not args.execute:
        preview(args, sample_count=len(rows))
        return 0
    from src.mvp.room_resolver import RoomResolver, RoomStatus
    def query(row):
        resolver = RoomResolver(timeout=4)
        try:
            r = resolver.resolve(row['entry'])
            return dict(id=row['id'], entry=row['entry'], status=r.status.value,
                        room_id=r.room_id, anchor_uid=r.anchor_uid), r.status == RoomStatus.UNKNOWN
        finally:
            resolver.session.close()
    failed = False
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        jobs = {pool.submit(query, row): row for row in rows}
        for future in as_completed(jobs):
            row = jobs[future]
            try:
                result, unknown = future.result()
                failed |= unknown
            except Exception as exc:
                result = dict(id=row['id'], entry=row['entry'], error=str(exc), status='unknown')
                failed = True
            print(json.dumps(result, ensure_ascii=False), flush=True)
    return 4 if failed else 0

if __name__ == '__main__':
    sys.exit(entry(main))
