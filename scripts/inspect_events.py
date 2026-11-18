from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import json
from collections import Counter
from scripts._common import entry, input_file

def read_events(path):
    raw = input_file(path).read_bytes()
    encoding = 'utf-16' if raw.startswith((b'\xff\xfe', b'\xfe\xff')) else 'utf-8-sig'
    events, invalid = [], []
    for index, line in enumerate(raw.decode(encoding).splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
            if not isinstance(event, dict) or not isinstance(event.get('event'), str):
                raise ValueError('缺少 event 字段')
            events.append(event)
        except (ValueError, TypeError) as exc:
            invalid.append(dict(line=index, error=str(exc)))
    return events, invalid

def main(argv=None):
    p = argparse.ArgumentParser(description='离线统计 NDJSON，不推导未记录的验收结论')
    p.add_argument('--input', required=True)
    args = p.parse_args(argv)
    events, invalid = read_events(args.input)
    keys = {'worker_started','worker_stopped','worker_failed','segment_started','segment_finished','segment_aborted'}
    print(json.dumps(dict(count=len(events), types=dict(Counter(e['event'] for e in events)),
                          invalid_lines=invalid, lifecycle=[e for e in events if e['event'] in keys]),
                     ensure_ascii=False, indent=2))
    return 4 if invalid or not events else 0

if __name__ == '__main__':
    sys.exit(entry(main))
