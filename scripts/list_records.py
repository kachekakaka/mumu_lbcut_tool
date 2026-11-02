from pathlib import Path
import os
import time

tmp_root = Path(r"D:\Projects\litleTools\mumu_lbcut_tool_dev\mumu_lbcut_tool_tmp")

print(f"{'目录类别':<18} | {'文件名':<35} | {'文件大小 (MB)':<12} | {'生成时间'}")
print("-" * 85)

for p in sorted(tmp_root.rglob("*.mp4")):
    rel_dir = p.parent.name
    size_mb = round(p.stat().st_size / (1024 * 1024), 2)
    mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
    print(f"{rel_dir:<18} | {p.name:<35} | {size_mb:>10} MB | {mtime}")
