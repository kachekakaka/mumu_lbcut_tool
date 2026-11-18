import xml.etree.ElementTree as ET

tree = ET.parse("scripts/ui.xml")
root = tree.getroot()

print("所有 y < 300 的节点:")
for n in root.iter("node"):
    bounds = n.attrib.get("bounds", "")
    text = n.attrib.get("text", "")
    desc = n.attrib.get("content-desc", "")
    rid = n.attrib.get("resource-id", "")
    cls = n.attrib.get("class", "")
    if bounds:
        # [x1, y1][x2, y2]
        import re
        m = re.findall(r"\d+", bounds)
        if len(m) == 4:
            x1, y1, x2, y2 = map(int, m)
            if y2 < 300:
                print(f"bounds={bounds}, text={text}, desc={desc}, rid={rid}, cls={cls}")
