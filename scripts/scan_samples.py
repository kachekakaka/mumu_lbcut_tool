"""并发扫描 31 个样本房间的开播状态与解析。"""
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.mvp.room_resolver import RoomResolver as WorkerRoomResolver, RoomStatus


def scan_single(r, resolver):
    res = resolver.resolve(r["entry"])
    return r, res


def main():
    resolver = WorkerRoomResolver(timeout=4)
    fixtures_path = REPO_ROOT / "tests" / "fixtures" / "sample_rooms.json"
    with open(fixtures_path, "r", encoding="utf-8") as f:
        rooms = json.load(f)

    print("=== 开始并发扫描用户提供的 31 个直播间 ===", flush=True)
    live_rooms = []
    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = [executor.submit(scan_single, r, resolver) for r in rooms]
        for f in as_completed(futures):
            r, res = f.result()
            status_str = "🟢 直播中" if res.status == RoomStatus.LIVE else ("🔴 未开播" if res.status == RoomStatus.ENDED else "⚪ 未知")
            print(f"[{r['id']:02d}] {r['nickname']} ({r['entry']}): {status_str} | room_id={res.room_id}", flush=True)
            if res.status == RoomStatus.LIVE:
                live_rooms.append((r, res))

    print(f"\n扫描完成！当前在播主播数: {len(live_rooms)} / {len(rooms)}", flush=True)
    for r, res in live_rooms:
        print(f" -> {r['nickname']} ({r['url']}) | room_id={res.room_id}", flush=True)


if __name__ == "__main__":
    main()
