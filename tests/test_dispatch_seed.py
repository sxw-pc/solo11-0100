from pathlib import Path
import pytest
from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.dispatch import register
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_dispatch_case_freezes_accepted_evidence(tmp_path):
    db = tmp_path / 'dispatch.db'
    freeze_config(db, FIX / 'dispatch-config.json')
    assert import_file(db, FIX / 'dispatch-events.jsonl')['imported'] == 15
    assert evaluate(db) == {'B1': 'accepted', 'B2': 'accepted', 'B3': 'accepted'}
    expected = {'case_id': 'dispatch-01', 'boxes': 3, 'vehicles': 3}
    assert register(db, 'dispatch-01', FIX / 'dispatch-case.json') == expected
    assert register(db, 'dispatch-01', FIX / 'dispatch-case.json') == expected
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM dispatch_case').fetchone()[0] == 1
    finally:
        conn.close()


def test_dispatch_case_rejects_unverified_boxes(tmp_path):
    db = tmp_path / 'dispatch.db'
    freeze_config(db, FIX / 'dispatch-config.json')
    with pytest.raises(ValueError, match='dispatch_box_not_accepted'):
        register(db, 'dispatch-01', FIX / 'dispatch-case.json')
