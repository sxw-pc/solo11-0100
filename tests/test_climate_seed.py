from pathlib import Path
import pytest
from dockscan.ingest import freeze_config, import_file
from dockscan.rules import evaluate
from dockscan.climate_control import register
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'


def test_climate_case_freezes_real_docks_and_sensors(tmp_path):
    db = tmp_path / 'climate.db'
    freeze_config(db, FIX / 'climate-config.json')
    assert import_file(db, FIX / 'climate-events.jsonl')['imported'] == 10
    assert evaluate(db) == {'B1': 'accepted', 'B2': 'accepted'}
    expected = {'case_id': 'climate-01', 'docks': 2, 'periods': 2}
    assert register(db, 'climate-01', FIX / 'climate-case.json') == expected
    assert register(db, 'climate-01', FIX / 'climate-case.json') == expected
    conn = connect(db)
    try:
        assert conn.execute('SELECT COUNT(*) FROM climate_case').fetchone()[0] == 1
    finally:
        conn.close()


def test_climate_case_requires_source_events(tmp_path):
    db = tmp_path / 'climate.db'
    freeze_config(db, FIX / 'climate-config.json')
    with pytest.raises(ValueError, match='climate_dock_events_missing'):
        register(db, 'climate-01', FIX / 'climate-case.json')
