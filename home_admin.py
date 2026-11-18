#!/usr/bin/env python3
"""Local operator action: release a reviewed unknown without claiming success."""
import argparse, os, re
from pathlib import Path
from home_relay import Store

parser=argparse.ArgumentParser(description=__doc__)
parser.add_argument('request_id')
parser.add_argument('--reason',required=True,help='short non-secret reason, lowercase letters, numbers and underscores')
args=parser.parse_args()
if not re.fullmatch(r'[a-z0-9_]{1,40}',args.reason): raise SystemExit('invalid reason')
if not Store(Path(os.environ['HOME_RELAY_DB'])).resolve_unknown(args.request_id,args.reason): raise SystemExit('request is not unknown')
print('review recorded; outcome remains unknown')
