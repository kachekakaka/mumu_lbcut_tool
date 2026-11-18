from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts._common import entry
from scripts._device import device_main

def main(argv=None):
    return device_main('screenshot', argv)

if __name__ == '__main__':
    sys.exit(entry(main))
