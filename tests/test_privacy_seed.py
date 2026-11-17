from pathlib import Path
import pytest
from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.privacy import register
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_privacy_case_freezes_three_real_confidential_rows(tmp_path):
    db = tmp_path / 'privacy.db'
    freeze_config(db, FIX / 'privacy-config.json')
    assert import_file(db, FIX / 'privacy-events.jsonl')['imported'] == 15
    assert evaluate(db) == {'B1': 'accepted', 'B2': 'accepted', 'B3': 'accepted'}
    expected = {'case_id': 'privacy-01', 'boxes': 3, 'prior_releases': 1}
    assert register(db, 'privacy-01', FIX / 'privacy-case.json') == expected
    assert register(db, 'privacy-01', FIX / 'privacy-case.json') == expected
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM privacy_case').fetchone()[0] == 1
    finally:
        conn.close()


def test_privacy_case_requires_original_rate_rows(tmp_path):
    db = tmp_path / 'privacy.db'
    freeze_config(db, FIX / 'privacy-config.json')
    with pytest.raises(ValueError, match='confidential_rate_source_missing'):
        register(db, 'privacy-01', FIX / 'privacy-case.json')
