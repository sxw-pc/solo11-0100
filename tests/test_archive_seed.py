import hashlib
from pathlib import Path
import pytest
from dockscan.ingest import freeze_config, import_file
from dockscan.archive import register
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_archive_case_freezes_two_distinct_overlapping_sources(tmp_path):
    db = tmp_path / 'archive.db'
    freeze_config(db, FIX / 'config.json')
    assert import_file(db, FIX / 'archive-source-a.jsonl')['imported'] == 25
    assert import_file(db, FIX / 'archive-source-b.jsonl')['imported'] == 1
    a = (FIX / 'archive-source-a.jsonl').read_bytes()
    b = (FIX / 'archive-source-b.jsonl').read_bytes()
    assert a != b and b.endswith(a)
    expected = {'case_id': 'archive-01', 'sources': 2}
    assert register(db, 'archive-01', FIX / 'archive-case.json') == expected
    assert register(db, 'archive-01', FIX / 'archive-case.json') == expected
    conn = connect(db)
    try:
        rows = conn.execute('SELECT sha256,raw FROM source ORDER BY id').fetchall()
        assert len(rows) == 2
        assert [r['sha256'] for r in rows] == [hashlib.sha256(x).hexdigest() for x in (a, b)]
    finally:
        conn.close()


def test_archive_case_requires_imported_sources(tmp_path):
    db = tmp_path / 'archive.db'
    freeze_config(db, FIX / 'config.json')
    with pytest.raises(ValueError, match='archive_source_missing'):
        register(db, 'archive-01', FIX / 'archive-case.json')

