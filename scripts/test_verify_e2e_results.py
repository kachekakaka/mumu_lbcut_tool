import json
from pathlib import Path
import subprocess

def test_e2e_results():
    base_dir = Path(r"..\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp\verify_e2e_overlap")
    events_file = base_dir / "captured_events.json"
    assert events_file.exists(), f"未找到事件记录文件: {events_file}"

    with open(events_file, "r", encoding="utf-8") as f:
        events = json.load(f)

    started_events = [e for e in events if e.get("event") == "segment_started"]
    finished_events = [e for e in events if e.get("event") == "segment_finished"]
    aborted_events = [e for e in events if e.get("event") == "segment_aborted"]

    print(f"事件统计: started={len(started_events)}, finished={len(finished_events)}, aborted={len(aborted_events)}")
    assert len(aborted_events) == 0, f"存在中止切片: {aborted_events}"
    assert len(started_events) == 3, f"预期 3 个 started 切片，实际: {len(started_events)}"
    assert len(finished_events) == 3, f"预期 3 个 finished 切片，实际: {len(finished_events)}"

    event_order = [(e.get("event"), e.get("segment_index")) for e in events if e.get("event") in ("segment_started", "segment_finished")]
    print(f"切片事件序列: {event_order}")

    # 验证相邻切片的交叠预热属性: started(i) -> started(i+1) -> finished(i)
    for i in range(1, len(started_events)):
        idx_s_i = event_order.index(("segment_started", i))
        idx_s_next = event_order.index(("segment_started", i + 1))
        idx_f_i = event_order.index(("segment_finished", i))
        assert idx_s_i < idx_s_next < idx_f_i, f"切片 {i} 与 {i+1} 未满足交叠属性"
        print(f"  [验证通过] 切片 #{i} -> 切片 #{i+1} 成功提前预热交叠捕获！")

    # 验证 3 个实际 MP4 产物
    ffprobe = Path(r"C:\Users\73839\.agent-reach-venv\Scripts\ffprobe.EXE")
    for e in finished_events:
        f_path = Path(e.get("file_path"))
        assert f_path.exists(), f"切片文件不存在: {f_path}"
        res = subprocess.run([str(ffprobe), "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(f_path)], capture_output=True, text=True)
        data = json.loads(res.stdout)
        fmt = data.get("format", {})
        dur = float(fmt.get("duration", 0))
        size_mb = int(fmt.get("size", 0)) / (1024 * 1024)
        v = [s.get("codec_name") for s in data.get("streams", []) if s.get("codec_type") == "video"]
        a = [s.get("codec_name") for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        print(f"  [产物检验] {f_path.name}: 大小={size_mb:.2f}MB, 时长={dur:.2f}s, 视频轨={v}, 音频轨={a}")
        assert "h264" in v, "缺少 H264 视频轨"
        assert "aac" in a, "缺少 AAC 音频轨"
        assert dur > 1.0, "时长过短"

    print("\n🎉 实机端到端交叠切片自动化核验 100% 通过！")

if __name__ == "__main__":
    test_e2e_results()
