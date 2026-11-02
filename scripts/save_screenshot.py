import subprocess
from pathlib import Path

adb = r"D:\soft\MuMuPlayer\nx_main\adb.exe"
serial = "127.0.0.1:16448"

subprocess.run([adb, "connect", serial], capture_output=True)

# 抓取当前屏幕
res = subprocess.run([adb, "-s", serial, "exec-out", "screencap", "-p"], capture_output=True)
out_path = Path("scripts/current_screen.png")
with open(out_path, "wb") as f:
    f.write(res.stdout)

print(f"截图保存成功: {out_path}, 大小: {out_path.stat().st_size} 字节")
