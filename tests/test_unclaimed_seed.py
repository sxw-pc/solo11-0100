from pathlib import Path
import pytest
from dockscan.ingest import freeze_config, import_file
from dockscan.unclaimed import register
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_unclaimed_pallet_case_freezes_actual_box_source(tmp_path):
    db = tmp_path / 'unclaimed.db'
    freeze_config(db, FIX / 'config.json')
    assert import_file(db, FIX / 'events.jsonl')['imported'] == 5
    expected = {'case_id': 'unclaimed-01', 'boxes': 1, 'claims': 2}
    assert register(db, 'unclaimed-01', FIX / 'unclaimed-case.json') == expected
    assert register(db, 'unclaimed-01', FIX / 'unclaimed-case.json') == expected
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM unclaimed_case').fetchone()[0] == 1
    finally:
        conn.close()


def test_unclaimed_pallet_requires_original_events(tmp_path):
    db = tmp_path / 'unclaimed.db'
    freeze_config(db, FIX / 'config.json')
    with pytest.raises(ValueError, match='unclaimed_source_events_missing'):
        register(db, 'unclaimed-01', FIX / 'unclaimed-case.json')
