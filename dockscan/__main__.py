import argparse
import json
import sys
import tempfile
from pathlib import Path

from .ingest import freeze_config, import_file
from .rules import evaluate
from .report import export
from .store import connect


def demo(workdir=None):
    root = Path(workdir or tempfile.mkdtemp(prefix='dockscan-'))
    root.mkdir(parents=True, exist_ok=True)
    db = root / 'ledger.db'
    base = Path(__file__).parent.parent / 'fixtures'
    freeze_config(db, base / 'config.json')
    import_file(db, base / 'events.jsonl')
    evaluate(db)
    return export(db, root / 'report.json')


def _open(db_path):
    return connect(db_path)


def cmd_archive(args):
    from .archive import migrate
    result = migrate(args.db, args.representation, store_root=args.blocks,
                     release=not args.no_release, collect=not args.no_gc,
                     vacuum_after=not args.no_vacuum)
    keep = {
        'generation': result['generation'],
        'representation': result['representation'],
        'verification_ok': result['verification']['ok'],
        'physical_before': result['physical_before'],
        'physical_after': result['physical_after'],
        'release': result['release'],
        'gc': {k: result['gc'][k] for k in ('removed_blocks', 'removed_bytes',
                                            'removed_tmp_files', 'removed_tmp_bytes',
                                            'closed_dead_readers')},
    }
    print(json.dumps(keep, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_verify(args):
    from .archive import active_generation, store_root_for, verify_generation
    db = _open(args.db)
    try:
        gen = args.generation or active_generation(db)
        if gen is None:
            raise SystemExit('no active generation')
        result = verify_generation(db, store_root_for(db, args.blocks), gen)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result['ok']:
        raise SystemExit(1)


def cmd_source(args):
    from .archive import read_source, store_root_for
    db = _open(args.db)
    try:
        if args.path is not None:
            row = db.execute('SELECT id FROM source WHERE path=? ORDER BY id LIMIT 1',
                             (args.path,)).fetchone()
            if row is None:
                raise SystemExit('source_missing')
            source_id = row['id']
        else:
            source_id = args.source_id
        raw = read_source(db, source_id, store_root=store_root_for(db, args.blocks))
    finally:
        db.close()
    if args.out:
        Path(args.out).write_bytes(raw)
        print(json.dumps({'source_id': source_id, 'bytes': len(raw), 'out': args.out}))
    else:
        sys.stdout.buffer.write(raw)


def cmd_gc(args):
    from .archive import gc, store_root_for
    db = _open(args.db)
    try:
        result = gc(db, store_root_for(db, args.blocks), dry_run=args.dry_run)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_storage(args):
    from .archive import accounting, store_root_for
    db = _open(args.db)
    try:
        result = accounting(db, args.generation, store_root_for(db, args.blocks))
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))


def cmd_register(args):
    from .archive import register
    print(register(args.db, args.case, args.fixture))


def cmd_case_check(args):
    from .archive import check_case, store_root_for
    db = _open(args.db)
    try:
        result = check_case(db, store_root_for(db, args.blocks), args.case)
    finally:
        db.close()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    if not result['ok']:
        raise SystemExit(1)


def main():
    p = argparse.ArgumentParser(description='仓门扫描对账台 — 离线证据与业务决策')
    sub = p.add_subparsers(dest='cmd', required=True)
    d = sub.add_parser('demo'); d.add_argument('--workdir')
    a = sub.add_parser('ingest'); a.add_argument('--db', required=True); a.add_argument('--config', required=True); a.add_argument('events')
    e = sub.add_parser('inspect'); e.add_argument('--db', required=True)
    r = sub.add_parser('report'); r.add_argument('--db', required=True); r.add_argument('--out', required=True)
    arc = sub.add_parser('archive', help='内容寻址归档迁移')
    arc.add_argument('--db', required=True)
    arc.add_argument('--representation', choices=('fixed_64', 'record_chunks'), default='record_chunks')
    arc.add_argument('--blocks')
    arc.add_argument('--no-release', action='store_true')
    arc.add_argument('--no-gc', action='store_true')
    arc.add_argument('--no-vacuum', action='store_true')
    v = sub.add_parser('verify'); v.add_argument('--db', required=True); v.add_argument('--blocks')
    v.add_argument('--generation', type=int)
    s = sub.add_parser('source', help='按 source_id 或路径导出原始字节'); s.add_argument('--db', required=True)
    s.add_argument('--source-id', type=int); s.add_argument('--path'); s.add_argument('--blocks')
    s.add_argument('--out')
    g = sub.add_parser('gc'); g.add_argument('--db', required=True); g.add_argument('--blocks')
    g.add_argument('--dry-run', action='store_true')
    st = sub.add_parser('storage'); st.add_argument('--db', required=True); st.add_argument('--blocks')
    st.add_argument('--generation', type=int)
    rg = sub.add_parser('register'); rg.add_argument('--db', required=True); rg.add_argument('--case', required=True)
    rg.add_argument('fixture')
    cc = sub.add_parser('case-check', help='按冻结案例逐源复原并核对 ID/路径/长度/SHA')
    cc.add_argument('--db', required=True); cc.add_argument('--case', required=True)
    cc.add_argument('--blocks')
    args = p.parse_args()
    if args.cmd == 'demo': print(demo(args.workdir))
    elif args.cmd == 'ingest': freeze_config(args.db, args.config); print(import_file(args.db, args.events))
    elif args.cmd == 'inspect': print(json.dumps(evaluate(args.db), ensure_ascii=False, indent=2))
    elif args.cmd == 'report': print(export(args.db, args.out))
    elif args.cmd == 'archive': cmd_archive(args)
    elif args.cmd == 'verify': cmd_verify(args)
    elif args.cmd == 'source': cmd_source(args)
    elif args.cmd == 'gc': cmd_gc(args)
    elif args.cmd == 'storage': cmd_storage(args)
    elif args.cmd == 'register': cmd_register(args)
    elif args.cmd == 'case-check': cmd_case_check(args)


if __name__ == '__main__':
    main()
