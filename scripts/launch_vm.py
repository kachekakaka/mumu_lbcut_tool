"""启动并就绪 MuMu 实例。"""
import sys
from pathlib import Path
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

import logging
from src.mvp.mumu_client import MuMuClient

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

def main():
    vmindex = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    client = MuMuClient()
    logging.info(f"准备拉起 MuMu 实例 {vmindex}...")
    ok = client.launch_player(vmindex, timeout_sec=90)
    if not ok:
        logging.error(f"启动实例 {vmindex} 失败")
        sys.exit(1)
    serial = client.get_adb_serial(vmindex)
    logging.info(f"实例 {vmindex} 启动成功，ADB Serial: {serial}")
    client.connect_adb(vmindex)

if __name__ == "__main__":
    main()
