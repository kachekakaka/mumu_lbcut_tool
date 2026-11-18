from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import argparse
import json
from scripts._common import entry, input_file, probe, media_summary

def main(argv=None):
    p = argparse.ArgumentParser(description='检查单个媒体文件的轨道信息')
    p.add_argument('--input', required=True)
    args = p.parse_args(argv)
    path = input_file(args.input)
    report = media_summary(path, probe(path))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['has_video'] and report['has_audio'] and report['duration_sec'] else 4

if __name__ == '__main__':
    sys.exit(entry(main))
