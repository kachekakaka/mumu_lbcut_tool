import subprocess
import xml.etree.ElementTree as ET

adb = r"D:\soft\MuMuPlayer\nx_main\adb.exe"
serial = "127.0.0.1:16448"

subprocess.run([adb, "connect", serial], capture_output=True)
subprocess.run([adb, "-s", serial, "pull", "/data/local/tmp/ui.xml", "scripts/ui.xml"], capture_output=True)

try:
    tree = ET.parse("scripts/ui.xml")
    root = tree.getroot()
    for n in root.iter("node"):
        cls = n.attrib.get("class", "")
        rid = n.attrib.get("resource-id", "")
        desc = n.attrib.get("content-desc", "")
        text = n.attrib.get("text", "")
        bounds = n.attrib.get("bounds", "")
        if any(kw in (rid + desc + text + cls).lower() for kw in ["close", "back", "关闭", "返回", "cancel", "imageview", "button"]):
            print(f"Node: cls={cls}, rid={rid}, desc={desc}, text={text}, bounds={bounds}")
except Exception as e:
    print(f"解析失败: {e}")
