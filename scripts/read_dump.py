import re

with open("scripts/ui_dump.xml", "r", encoding="utf-8", errors="ignore") as f:
    content = f.read()

texts = re.findall(r'text="([^"]+)"', content)
print("提取到的文本 (前20个):", texts[:20])

descs = re.findall(r'content-desc="([^"]+)"', content)
print("提取到的描述 (前20个):", [d for d in descs if d.strip()][:20])
