from pathlib import Path
import pytest
from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.returns import register
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_return_case_freezes_accepted_split_source(tmp_path):
    db = tmp_path / 'returns.db'
    freeze_config(db, FIX / 'return-forward-config.json')
    assert import_file(db, FIX / 'return-forward-events.jsonl')['imported'] == 6
    assert evaluate(db)['B1'] == 'accepted'
    expected = {'case_id': 'return-01', 'parcels': 2, 'rmas': 4, 'lineage_edges': 2}
    assert register(db, 'return-01', FIX / 'return-case.json') == expected
    assert register(db, 'return-01', FIX / 'return-case.json') == expected
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM return_case').fetchone()[0] == 1
    finally:
        conn.close()


def test_return_case_requires_accepted_origin(tmp_path):
    db = tmp_path / 'returns.db'
    freeze_config(db, FIX / 'return-forward-config.json')
    with pytest.raises(ValueError, match='return_origin_not_accepted'):
        register(db, 'return-01', FIX / 'return-case.json')
