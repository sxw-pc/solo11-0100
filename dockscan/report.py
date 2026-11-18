import json
from pathlib import Path
from .store import connect


def export(db_path, out, store_root=None):
    """Write the JSON/Markdown report.

    Source rows still carry their immutable id/path/sha256; nothing about the
    archive changes this projection or the event/evidence chain.  When an
    archive is present the report adds the pinned-generation verification and
    honest physical storage accounting.
    """
    db = connect(db_path)
    try:
        data = {table: [dict(r) for r in db.execute(f'SELECT * FROM {table} ORDER BY 1')]
                for table in ('batch', 'subject', 'event', 'issue', 'decision', 'evidence', 'run', 'lineage')}
        data['source'] = [dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')]
        data['config_hash'] = db.execute("SELECT value FROM meta WHERE key='config_hash'").fetchone()['value']
        archive_block = None
        try:
            from .archive import active_generation, accounting, open_reader, sha256, store_root_for
            generation = active_generation(db)
            if generation is not None:
                root = store_root_for(db, store_root)
                reader = open_reader(db, root, generation)
                source_check = []
                try:
                    for item in data['source']:
                        try:
                            raw = reader.read(item['id']) if reader else None
                        except Exception as exc:
                            raw = None
                            source_check.append({'source_id': item['id'], 'ok': False,
                                                 'errors': [str(exc)]})
                            continue
                        if raw is None:
                            source_check.append({'source_id': item['id'], 'ok': False,
                                                 'errors': ['no_manifest']})
                            continue
                        digest = sha256(raw)
                        source_check.append({
                            'source_id': item['id'], 'path': item['path'],
                            'sha256': digest, 'length': len(raw),
                            'ok': digest == item['sha256'],
                        })
                finally:
                    if reader:
                        reader.close()
                last_release = db.execute(
                    "SELECT value FROM archive_state WHERE key='last_release'").fetchone()
                archive_block = {
                    'generation': generation,
                    'sources': source_check,
                    'ok': all(s['ok'] for s in source_check),
                    'accounting': accounting(db, generation, root),
                    'last_release': (json.loads(last_release['value'])
                                     if last_release and last_release['value'] else None),
                }
        except Exception as exc:  # report must still export business data
            archive_block = {'ok': False, 'error': str(exc)}
        if archive_block is not None:
            data['archive'] = archive_block
        target = Path(out)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
        md = target.with_suffix('.md')
        lines = ['# 决策报告', '']
        lines += [f"- {d['subject']}: {d['status']} — {d['details']}" for d in data['decision']]
        if archive_block is not None:
            lines += ['', '## 原件存储', '']
            lines.append(f"- 活跃世代: {archive_block.get('generation')}")
            if 'accounting' in archive_block:
                physical = archive_block['accounting']['physical']
                lines.append(f"- 块文件: {physical['block_files']} 个 / {physical['block_bytes']} 字节")
                lines.append(f"- 数据库文件: {physical['db_file_bytes']} 字节")
                lines.append(f"- 原 BLOB 残留: {physical['blob_bytes']} 字节")
            lines.append(f"- 逐源复原: {'通过' if archive_block.get('ok') else '失败'}")
        md.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        return target
    finally:
        db.close()
