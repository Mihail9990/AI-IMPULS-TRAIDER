"""Create a PRIVATE offline backup/review; never activate or connect to MT5."""
import argparse
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tradfi.native_migration import prepare_review

if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--legacy-state',required=True);p.add_argument('--legacy-config',required=True)
    p.add_argument('--destination',required=True);p.add_argument('--common-files-directory',required=True)
    p.add_argument('--native-magic',type=int,required=True)
    args=p.parse_args()
    result=prepare_review(args.legacy_state,args.legacy_config,args.destination,args.common_files_directory,native_magic=args.native_magic)
    print('Private backup/review created; activation is OFF. Review migration-review.json locally.')
    print('Unresolved commands:',result['unresolved_command_count'],'Open tickets:',result['open_ticket_count'])
