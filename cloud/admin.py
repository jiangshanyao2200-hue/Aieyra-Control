"""Server-shell-only moderation. This module is not imported by the web server."""
import argparse,json,sqlite3
from pathlib import Path
p=argparse.ArgumentParser();p.add_argument('--data',type=Path,default=Path('/srv/aieyra-control/data'))
s=p.add_subparsers(dest='action',required=True)
a=s.add_parser('hide');a.add_argument('seq',type=int)
a=s.add_parser('block');a.add_argument('subject');a.add_argument('--reason',required=True)
a=s.add_parser('unblock');a.add_argument('subject')
s.add_parser('status');args=p.parse_args()
with sqlite3.connect(args.data/'cloud.sqlite') as db:
 if args.action=='hide':db.execute('UPDATE posts SET hidden=1 WHERE seq=?',(args.seq,))
 elif args.action=='block':db.execute('INSERT OR REPLACE INTO blocked VALUES(?,?)',(args.subject,args.reason));db.execute('UPDATE sessions SET revoked=1 WHERE subject=?',(args.subject,))
 elif args.action=='unblock':db.execute('DELETE FROM blocked WHERE subject=?',(args.subject,))
 print(json.dumps({'action':args.action,'posts':db.execute('SELECT COUNT(*) FROM posts').fetchone()[0],'blocked':db.execute('SELECT COUNT(*) FROM blocked').fetchone()[0]}))
