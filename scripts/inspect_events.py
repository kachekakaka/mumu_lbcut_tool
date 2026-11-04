import json
from pathlib import Path

def main():
    events_file = Path(r"..\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\20261104-e2e-30min-soak\events.ndjson")
    if not events_file.exists():
        print("events.ndjson 不存在")
        return

    # 支持 PowerShell UTF-16LE 重定向编码
    raw_bytes = events_file.read_bytes()
    if raw_bytes.startswith(b"\xff\xfe") or raw_bytes.startswith(b"\xfe\xff"):
        text = raw_bytes.decode("utf-16")
    else:
        text = raw_bytes.decode("utf-8", errors="replace")

    events = []
    for line in text.splitlines():
        line = line.strip()
        if line:
            try:
                events.append(json.loads(line))
            except Exception:
                pass

    print(f"总事件数: {len(events)}")
    type_counts = {}
    for ev in events:
        t = ev.get("event")
        type_counts[t] = type_counts.get(t, 0) + 1

    print("\n事件类型分布:")
    for t, c in sorted(type_counts.items()):
        print(f"  {t}: {c}")

    print("\n生命周期关键事件序列:")
    for ev in events:
        t = ev.get("event")
        if t in ["worker_started", "segment_started", "segment_finished", "segment_aborted", "stream_ended"]:
            idx = ev.get("segment_index", "-")
            path = ev.get("file_path", "")
            reason = ev.get("reason", "")
            print(f"  [{ev.get('timestamp')}] {t} (segment #{idx}) reason={reason} path={Path(path).name if path else ''}")

if __name__ == "__main__":
    main()
