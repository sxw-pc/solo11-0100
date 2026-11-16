import argparse,json,tempfile
from pathlib import Path
from .ingest import freeze_config,import_file
from .rules import evaluate
from .review import sign,revoke
from .report import export
from .store import connect

def demo(workdir=None):
    root=Path(workdir or tempfile.mkdtemp(prefix='dockscan-'));root.mkdir(parents=True,exist_ok=True)
    db=root/'ledger.db'; base=Path(__file__).parent.parent/'fixtures'
    freeze_config(db,base/'config.json');import_file(db,base/'events.jsonl');evaluate(db)
    return export(db,root/'report.json')
def main():
    p=argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据与业务决策')
    sub=p.add_subparsers(dest='cmd',required=True)
    d=sub.add_parser('demo');d.add_argument('--workdir')
    a=sub.add_parser('ingest');a.add_argument('--db',required=True);a.add_argument('--config',required=True);a.add_argument('events')
    e=sub.add_parser('inspect');e.add_argument('--db',required=True)
    r=sub.add_parser('report');r.add_argument('--db',required=True);r.add_argument('--out',required=True)
    s=sub.add_parser('sign',help='复核员签署当前待核结论');s.add_argument('--db',required=True);s.add_argument('--subject',required=True);s.add_argument('--reviewer',required=True);s.add_argument('--reason',required=True)
    v=sub.add_parser('revoke',help='撤销既有签署（保留历史记录）');v.add_argument('--db',required=True);v.add_argument('--signoff',type=int,required=True);v.add_argument('--reviewer',required=True);v.add_argument('--reason',required=True)
    args=p.parse_args()
    try:
        if args.cmd=='demo': print(demo(args.workdir))
        elif args.cmd=='ingest': freeze_config(args.db,args.config);print(import_file(args.db,args.events))
        elif args.cmd=='inspect': print(json.dumps(evaluate(args.db),ensure_ascii=False,indent=2))
        elif args.cmd=='report': print(export(args.db,args.out))
        elif args.cmd=='sign': print(json.dumps(sign(args.db,args.subject,args.reviewer,args.reason),ensure_ascii=False))
        elif args.cmd=='revoke': print(json.dumps(revoke(args.db,args.signoff,args.reviewer,args.reason),ensure_ascii=False))
    except ValueError as exc:
        raise SystemExit(f'error: {exc}')
if __name__=='__main__':main()
