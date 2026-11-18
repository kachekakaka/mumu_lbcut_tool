from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import json
import re
import xml.etree.ElementTree as ET
from scripts._common import entry, input_file

def main(argv=None):
    p = argparse.ArgumentParser(description='离线读取 UI XML，不连接设备')
    p.add_argument('--input', required=True)
    p.add_argument('--filter', choices=['all', 'top', 'controls'], default='all')
    p.add_argument('--top-y', type=int, default=300)
    args = p.parse_args(argv)
    if args.top_y < 0:
        p.error('--top-y 不能为负数')
    root = ET.parse(input_file(args.input)).getroot()
    if root.tag != 'hierarchy':
        raise ValueError('不是 UI hierarchy XML')
    rows = []
    for node in root.iter('node'):
        row = dict(node.attrib)
        words = ' '.join(row.get(k, '') for k in ('text', 'content-desc', 'resource-id', 'class'))
        if args.filter == 'all' and not (row.get('text') or row.get('content-desc')):
            continue
        if args.filter == 'controls' and not any(k in words.lower() for k in ('close','back','关闭','返回','cancel','button','imageview')):
            continue
        if args.filter == 'top':
            coords = re.findall(r'-?\d+', row.get('bounds', ''))
            if len(coords) != 4 or int(coords[3]) >= args.top_y:
                continue
        rows.append(row)
    print(json.dumps(rows, ensure_ascii=False, indent=2))

if __name__ == '__main__':
    sys.exit(entry(main))
