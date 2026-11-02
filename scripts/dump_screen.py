import subprocess
import re
from pathlib import Path

adb = r"D:\soft\MuMuPlayer\nx_main\adb.exe"
serial = "127.0.0.1:16448"

subprocess.run([adb, "connect", serial], capture_output=True)

# 抓取当前界面结构
res = subprocess.run([adb, "-s", serial, "shell", "dumpsys", "window", "windows"], capture_output=True, text=True)
for line in res.stdout.splitlines():
    if "mCurrentFocus" in line or "mFocusedApp" in line:
        print(line.strip())

# 导出 uiautomator
subprocess.run([adb, "-s", serial, "shell", "uiautomator", "dump", "/data/local/tmp/ui.xml"], capture_output=True)
xml_res = subprocess.run([adb, "-s", serial, "shell", "cat", "/data/local/tmp/ui.xml"], capture_output=True, text=True, encoding="utf-8", errors="replace")

# 提取关键文字
texts = re.findall(r'text="([^"]+)"', xml_res.stdout)
print("界面提取文字:", [t for t in texts if t.strip()])
