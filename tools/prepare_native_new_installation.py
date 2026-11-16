"""Offline fresh-install review: no legacy database, no broker access, no activation."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tradfi.native_migration import prepare_new_installation
if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--settings',required=True,help='Explicit JSON: login/server/symbol/magic/common_files_directory/EA distances')
    p.add_argument('--destination',required=True)
    a=p.parse_args()
    prepare_new_installation(json.loads(Path(a.settings).read_text(encoding='utf-8-sig')),a.destination)
    print('Fresh-install review created; trading disabled. Broker flat check remains mandatory.')
