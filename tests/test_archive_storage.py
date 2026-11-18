"""Content-addressed archive, storage accounting and crash recovery tests."""
import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from dockscan import archive
from dockscan.archive import (
    BlockStore, accounting, active_generation, blob_usage, build, check_case, gc,
    generation_accounting, generation_layout, materialize, migrate, open_reader,
    original_bytes, physical_usage, plan_generation, publish, read_source,
    register, release_blobs, sha256, split_records, verify_generation,
)
from dockscan.ingest import freeze_config, import_file
from dockscan.report import export
from dockscan.rules import evaluate
from dockscan.store import connect

FIX = Path(__file__).resolve().parents[1] / 'fixtures'
A_PATH = FIX / 'archive-source-a.jsonl'
B_PATH = FIX / 'archive-source-b.jsonl'
A_RAW = A_PATH.read_bytes()
B_RAW = B_PATH.read_bytes()
A_SHA = hashlib.sha256(A_RAW).hexdigest()
B_SHA = hashlib.sha256(B_RAW).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class Crash(Exception):
    pass


def raiser(point):
    def hook(event, detail):
        if event == point:
            raise Crash(point)
    return hook


@pytest.fixture()
def workspace(tmp_path):
    """A DB with both overlapping fixture sources imported and a frozen case."""
    db_path = tmp_path / 'ledger.db'
    blocks = tmp_path / 'blocks'
    freeze_config(db_path, FIX / 'config.json')
    import_file(db_path, A_PATH)
    import_file(db_path, B_PATH)
    assert register(db_path, 'archive-01', FIX / 'archive-case.json') == {
        'case_id': 'archive-01', 'sources': 2}
    return {'db': db_path, 'blocks': blocks, 'root': tmp_path}


def connect_ws(ws):
    return connect(ws['db'])


# ---------------------------------------------------------------------------
# Lossless reconstruction
# ---------------------------------------------------------------------------

def test_fixture_is_really_overlapping():
    assert A_RAW != B_RAW and B_RAW.endswith(A_RAW)
    # B adds exactly one leading record; a whole-file SHA dedupe cannot help.
    assert B_RAW[:len(B_RAW) - len(A_RAW)].count(b'\n') == 1
    assert A_SHA != B_SHA


def test_both_representations_recover_each_source_byte_for_byte(workspace):
    for representation in ('fixed_64', 'record_chunks'):
        result = migrate(workspace['db'], representation, store_root=workspace['blocks'])
        assert result['verification']['ok']
        db = connect_ws(workspace)
        try:
            assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
            assert read_source(db, 2, store_root=workspace['blocks']) == B_RAW
            for item in result['verification']['sources']:
                assert item['reconstructed_sha256'] == item['sha256']
        finally:
            db.close()
        assert hashlib.sha256(read_source(connect_ws(workspace), 1,
                                          store_root=workspace['blocks'])).hexdigest() == A_SHA


@pytest.mark.parametrize('representation', ['fixed_64', 'record_chunks'])
def test_bom_crlf_no_trailing_newline_non_ascii_and_duplicate_lines(tmp_path, representation):
    """Every byte, including delimiters, BOM and a missing final newline."""
    db_path = tmp_path / 'ledger.db'
    blocks = tmp_path / 'blocks'
    freeze_config(db_path, FIX / 'config.json')
    record = (b'{"kind":"weight","subject":"B1","identity":"D1:6",'
              b'"time":"2026-01-01T08:30:00Z","dock":"D1","note":"\xe4\xbb\x93\xe4\xbd\x8d"}')
    raw = (b'\xef\xbb\xbf' + record + b'\r\n' + record + b'\r\n' + record + b'\r\n'
           + b'{"kind":"exit","subject":"B1","identity":"D1:9",'
             b'"time":"2026-01-01T08:29:00Z","dock":"D1"}')  # no final newline
    path = tmp_path / 'mixed.jsonl'
    path.write_bytes(raw)
    import_file(db_path, path)
    import_file(db_path, A_PATH)
    result = migrate(db_path, representation, store_root=blocks)
    assert result['verification']['ok']
    db = connect(db_path)
    try:
        restored = read_source(db, 1, store_root=blocks)
        assert restored == raw
        assert restored[:3] == b'\xef\xbb\xbf'
        assert restored.count(b'\r\n') == 3
        assert not restored.endswith(b'\n')
        assert read_source(db, 2, store_root=blocks) == A_RAW
        layout = generation_layout(db, result['generation'], source_id=1)[0]
        if representation == 'record_chunks':
            assert layout['prefix_bytes'] == 3
            assert layout['suffix_bytes'] == len(
                b'{"kind":"exit","subject":"B1","identity":"D1:9",'
                b'"time":"2026-01-01T08:29:00Z","dock":"D1"}')
        else:
            # fixed_64 has no side channel: the BOM lives in the first block.
            assert layout['prefix_bytes'] == 0 and layout['suffix_bytes'] == 0
    finally:
        db.close()


def test_record_chunks_are_raw_records_with_actual_delimiters():
    prefix, blocks, suffix = split_records(A_RAW)
    assert prefix == b'' and suffix == b''
    assert b''.join(blocks) == A_RAW
    assert blocks[0].endswith(b'\r\n')
    assert blocks[5].endswith(b'\n') and not blocks[5].endswith(b'\r\n')
    # A file without a final delimiter keeps the last record as the suffix.
    prefix, blocks, suffix = split_records(b'a\nb')
    assert (prefix, blocks, suffix) == (b'', [b'a\n'], b'b')


def test_manifest_reading_never_reparses_json(workspace):
    """Corrupting a block must fail; reading must not silently re-serialize."""
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        manifest = db.execute(
            'SELECT * FROM source_manifest WHERE source_id=1').fetchone()
        blocks = db.execute(
            'SELECT * FROM manifest_block WHERE manifest_id=? ORDER BY ordinal',
            (manifest['id'],)).fetchall()
        path = BlockStore(workspace['blocks']).path(blocks[0]['block_sha'])
        raw = bytearray(path.read_bytes())
        raw[0] = ord('[')  # still valid-ish text, no longer the original bytes
        path.write_bytes(bytes(raw))
        with pytest.raises(ValueError, match='block_corrupt'):
            materialize(db, workspace['blocks'], manifest=manifest)
        with pytest.raises(ValueError, match='block_corrupt'):
            read_source(db, 1, store_root=workspace['blocks'])
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Block sharing and boundary behaviour
# ---------------------------------------------------------------------------

def test_record_chunks_reuse_the_25_unchanged_records(workspace):
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    layout = {item['source_id']: item for item in
              generation_accounting(workspace['db'] and connect_ws(workspace),
                                    result['generation'])['layout']}
    assert layout[1]['block_count'] == 25
    assert layout[2]['block_count'] == 26
    shared = set(layout[1]['block_hashes']) & set(layout[2]['block_hashes'])
    assert len(shared) == 25
    gen = generation_accounting(connect_ws(workspace), result['generation'])
    assert gen['unique_blocks'] == 26
    assert gen['logical_block_refs'] == 51
    assert gen['unique_block_bytes'] == len(B_RAW)
    assert gen['unique_blocks'] < gen['logical_block_refs']


def test_fixed_64_boundaries_shift_and_share_almost_nothing(workspace):
    migrate(workspace['db'], 'fixed_64', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        gen = generation_accounting(db, active_generation(db))
        layout = {item['source_id']: item for item in gen['layout']}
        assert layout[1]['block_count'] == 42  # ceil(2655 / 64)
        assert layout[2]['block_count'] == 44  # ceil(2761 / 64)
        shared = set(layout[1]['block_hashes']) & set(layout[2]['block_hashes'])
        # One 106-byte insertion dislocates nearly every 64-byte boundary.
        assert len(shared) <= 2
        assert gen['unique_blocks'] >= 84
        assert gen['unique_block_bytes'] > len(A_RAW) + len(B_RAW) - 200
    finally:
        db.close()


def test_two_representations_have_different_layout_and_cost(workspace):
    first = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        record = accounting(db, first['generation'], workspace['blocks'])
    finally:
        db.close()
    second = migrate(workspace['db'], 'fixed_64', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        fixed = accounting(db, second['generation'], workspace['blocks'])
    finally:
        db.close()
    assert record['generation']['unique_blocks'] == 26
    assert fixed['generation']['unique_blocks'] >= 84
    assert record['generation']['unique_block_bytes'] < fixed['generation']['unique_block_bytes']
    assert record['generation']['manifest_bytes'] != fixed['generation']['manifest_bytes']
    # Physical bytes are real; logical references are reported separately.
    assert record['physical']['block_bytes'] == record['generation']['unique_block_bytes']
    assert record['manifest_block_refs'] == 51
    assert record['manifest_rows'] == 2


def test_same_content_from_different_sources_shares_blocks(tmp_path):
    db_path = tmp_path / 'ledger.db'
    blocks = tmp_path / 'blocks'
    freeze_config(db_path, FIX / 'config.json')
    copy = tmp_path / 'copy-of-a.jsonl'
    copy.write_bytes(A_RAW)
    import_file(db_path, A_PATH)
    import_file(db_path, copy)  # distinct path, distinct source id, same bytes... deduped by sha
    db = connect(db_path)
    # Source import de-duplicates identical file hashes: only one source row.
    assert db.execute('SELECT count(*) FROM source').fetchone()[0] == 1
    db.close()


def test_distinct_files_with_identical_records_share_physical_blocks(tmp_path):
    db_path = tmp_path / 'ledger.db'
    blocks = tmp_path / 'blocks'
    freeze_config(db_path, FIX / 'config.json')
    # Two files with different bytes but heavily overlapping records.
    first = A_RAW
    second = b'{"kind":"gate","subject":"B1","identity":"D1:77",' \
             b'"time":"2026-01-01T08:00:00Z","dock":"D1"}\n' + A_RAW
    p1, p2 = tmp_path / 'one.jsonl', tmp_path / 'two.jsonl'
    p1.write_bytes(first)
    p2.write_bytes(second)
    import_file(db_path, p1)
    import_file(db_path, p2)
    result = migrate(db_path, 'record_chunks', store_root=blocks)
    assert result['verification']['ok']
    db = connect(db_path)
    try:
        gen = generation_accounting(db, result['generation'])
        layout = {item['source_id']: item for item in gen['layout']}
        assert len(set(layout[1]['block_hashes']) & set(layout[2]['block_hashes'])) == 25
        assert gen['unique_blocks'] == 26
        assert read_source(db, 1, store_root=blocks) == first
        assert read_source(db, 2, store_root=blocks) == second
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Identity and reporting compatibility
# ---------------------------------------------------------------------------

def test_source_identity_events_and_evidence_survive_switch_and_release(workspace):
    evaluate(workspace['db'])
    db = connect_ws(workspace)
    try:
        before_sources = [dict(r) for r in db.execute(
            'SELECT id,path,sha256 FROM source ORDER BY id')]
        before_events = [dict(r) for r in db.execute('SELECT * FROM event ORDER BY id')]
        before_evidence = [dict(r) for r in db.execute('SELECT * FROM evidence ORDER BY 1,2')]
        before_decisions = [dict(r) for r in db.execute('SELECT * FROM decision ORDER BY 1')]
        before_issues = [dict(r) for r in db.execute(
            'SELECT code,detail FROM issue ORDER BY 1,2')]
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['release']['blob_after'] == 0
    db = connect_ws(workspace)
    try:
        assert [dict(r) for r in db.execute(
            'SELECT id,path,sha256 FROM source ORDER BY id')] == before_sources
        assert [dict(r) for r in db.execute('SELECT * FROM event ORDER BY id')] == before_events
        assert [dict(r) for r in db.execute('SELECT * FROM evidence ORDER BY 1,2')] == before_evidence
        assert [dict(r) for r in db.execute('SELECT * FROM decision ORDER BY 1')] == before_decisions
        assert [dict(r) for r in db.execute(
            'SELECT code,detail FROM issue ORDER BY 1,2')] == before_issues
        # Foreign keys still resolve to the same source rows.
        assert db.execute('SELECT count(*) FROM event e JOIN source s ON s.id=e.source_id'
                          ).fetchone()[0] == len(before_events)
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        check = check_case(db, workspace['blocks'], 'archive-01')
        assert check['ok']
        assert {c['sha256'] for c in check['sources']} == {A_SHA, B_SHA}
    finally:
        db.close()


def test_report_keeps_source_summary_and_adds_physical_accounting(workspace, tmp_path):
    evaluate(workspace['db'])
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    out = export(workspace['db'], tmp_path / 'report.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert [(s['id'], s['path'], s['sha256']) for s in data['source']] == [
        (1, str(A_PATH), A_SHA), (2, str(B_PATH), B_SHA)]
    assert data['archive']['ok']
    assert data['archive']['generation'] == active_generation(connect_ws(workspace))
    accounting_block = data['archive']['accounting']
    assert accounting_block['physical']['blob_bytes'] == 0
    assert accounting_block['physical']['block_bytes'] == len(B_RAW)
    assert accounting_block['generation']['unique_blocks'] == 26
    # The old evidence chain is unchanged.
    assert data['evidence'] and all(e['event_id'] in {x['id'] for x in data['event']}
                                    for e in data['evidence'])
    assert out.with_suffix('.md').exists()


def test_report_before_any_archive_has_no_archive_block(workspace, tmp_path):
    out = export(workspace['db'], tmp_path / 'pre.json')
    data = json.loads(out.read_text(encoding='utf-8'))
    assert 'archive' not in data
    assert len(data['source']) == 2


# ---------------------------------------------------------------------------
# Physical accounting honesty
# ---------------------------------------------------------------------------

def test_tampered_legacy_blob_blocks_release(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'], release=False)
    db = connect_ws(workspace)
    try:
        gen = active_generation(db)
        db.execute('UPDATE source SET raw=? WHERE id=1', (b'tampered',))
        db.commit()
        with pytest.raises(ValueError, match='archive_verification_failed'):
            release_blobs(db, gen, workspace['blocks'])
        assert blob_usage(db)['blob_sources'] == 2
        # Restoring the original bytes makes release safe again.
        db.execute('UPDATE source SET raw=? WHERE id=1', (A_RAW,))
        db.commit()
        assert release_blobs(db, gen, workspace['blocks'])['blob_after'] == 0
    finally:
        db.close()


def test_physical_accounting_is_not_logical_reference_counting(workspace):
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    release = result['release']
    db = connect_ws(workspace)
    try:
        usage = physical_usage(db, workspace['blocks'])
    finally:
        db.close()
    assert release['blob_before'] == len(A_RAW) + len(B_RAW)  # 5416 logical bytes
    assert release['blob_after'] == 0
    # Whole-file SHA dedupe would have saved nothing; record chunks physically
    # store only one copy of the shared bytes.
    assert usage['block_bytes'] == len(B_RAW)
    assert release['blob_logical_reclaimed'] == len(A_RAW) + len(B_RAW)
    # Reported physical bytes are block files plus real DB/WAL files.
    assert usage['physical_bytes'] == (usage['block_bytes'] + usage['db_file_bytes']
                                       + usage['db_wal_bytes'])
    assert usage['physical_bytes'] < len(A_RAW) + len(B_RAW) + usage['db_file_bytes'] + 65536
    # Before/after numbers are recorded and non-negative after VACUUM.
    assert release['vacuumed']
    assert release['physical_before']['blob_bytes'] == len(A_RAW) + len(B_RAW)
    assert release['physical_after']['blob_bytes'] == 0


def test_gc_never_reports_logical_refs_as_reclaimed_disk(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        db.execute('DELETE FROM archive_reader')
        result = gc(db, workspace['blocks'], dry_run=True)
        assert result['removed_bytes'] == 0 and result['dry_run']
        real = gc(db, workspace['blocks'])
        assert real['removed_bytes'] == 0
        # Only genuinely unreferenced block files are counted.
        gen = generation_accounting(db, active_generation(db))
        assert gen['unique_block_bytes'] == len(B_RAW)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Online migration: pins, CAS publication, new imports
# ---------------------------------------------------------------------------

def test_reader_pins_generation_across_switch_and_gc(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    old_reader = open_reader(db, workspace['blocks'])
    pinned = old_reader.generation_id
    other = connect_ws(workspace)
    try:
        gen, expected = plan_generation(other, 'fixed_64')
        build(other, workspace['blocks'], gen)
        publish(other, gen, workspace['blocks'], expected_pointer=expected)
        assert active_generation(other) != pinned
        # The pinned reader keeps returning the exact old-generation bytes.
        assert old_reader.read(1) == A_RAW
        assert old_reader.read(2) == B_RAW
        pinned_result = gc(other, workspace['blocks'])
        assert pinned_result['removed_blocks'] == 0
        assert pinned in pinned_result['protected_generations']
    finally:
        old_reader.close()
        other.close()
    db2 = connect_ws(workspace)
    try:
        after = gc(db2, workspace['blocks'])
        assert after['removed_blocks'] == 26
        assert after['removed_bytes'] == len(B_RAW)
        assert read_source(db2, 1, store_root=workspace['blocks']) == A_RAW
        assert read_source(db2, 2, store_root=workspace['blocks']) == B_RAW
    finally:
        db2.close()
        db.close()


def test_publish_is_compare_and_swap(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        first = active_generation(db)
        gen, expected = plan_generation(db, 'fixed_64')
        assert expected == first
        build(db, workspace['blocks'], gen)
        # Another writer switches the generation first.
        other_gen, other_expected = plan_generation(db, 'fixed_64')
        assert other_gen == gen  # resumed plan, not a new one
        publish(db, gen, workspace['blocks'], expected_pointer=expected)
        with pytest.raises(ValueError, match='generation_pointer_conflict'):
            publish(db, gen, workspace['blocks'], expected_pointer=first)
        assert active_generation(db) == gen
    finally:
        db.close()


def test_new_import_after_switch_gets_a_manifest_and_is_readable(workspace, tmp_path):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    pinned_db = connect_ws(workspace)
    pinned = open_reader(pinned_db, workspace['blocks'])
    old_gen = pinned.generation_id
    new_path = tmp_path / 'late.jsonl'
    new_path.write_bytes(
        b'{"kind":"gate","subject":"B1","identity":"D1:99",'
        b'"time":"2026-01-01T08:06:00Z","dock":"D1"}\n')
    result = import_file(workspace['db'], new_path, store_root=workspace['blocks'])
    assert result['imported'] == 1 and result['archived']['switched']
    db = connect_ws(workspace)
    try:
        assert read_source(db, result['source_id'], store_root=workspace['blocks']) \
            == new_path.read_bytes()
        assert active_generation(db) != old_gen
        # The pinned reader cannot see the new source but still reads the old two.
        assert pinned.read(1) == A_RAW
        assert pinned.read(result['source_id']) is None
        assert gc(db, workspace['blocks'])['removed_blocks'] == 0
        pinned.close()
        # The new generation carries the old manifests forward by reference,
        # so the shared blocks remain reachable and GC rightly keeps them.
        assert gc(db, workspace['blocks'])['removed_blocks'] == 0
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
    finally:
        pinned.close()
        pinned_db.close()
        db.close()


def test_concurrent_readers_do_not_see_a_partial_generation(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    other = connect_ws(workspace)
    try:
        readers = [open_reader(db, workspace['blocks']) for _ in range(3)]
        gen, expected = plan_generation(other, 'fixed_64')
        build(other, workspace['blocks'], gen)
        # Not yet published: every reader still resolves the old generation.
        assert all(r.generation_id != gen for r in readers)
        assert all(r.read(1) == A_RAW for r in readers)
        publish(other, gen, workspace['blocks'], expected_pointer=expected)
        assert all(r.read(1) == A_RAW and r.read(2) == B_RAW for r in readers)
        fresh = open_reader(db, workspace['blocks'])
        assert fresh.generation_id == gen
        assert fresh.read(1) == A_RAW
        fresh.close()
    finally:
        for r in readers:
            r.close()
        db.close()
        other.close()


# ---------------------------------------------------------------------------
# Crash points / idempotent restart
# ---------------------------------------------------------------------------

CRASH_POINTS = ['block_staged', 'manifest_written', 'manifests_written',
                'published', 'blobs_cleared', 'blobs_released']


@pytest.mark.parametrize('point', CRASH_POINTS)
def test_crash_at_each_stage_restarts_idempotently(workspace, point):
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser(point))
    # Whatever happened, the frozen case is still verifiable: either the legacy
    # BLOBs are intact or a published generation exists.
    db = connect_ws(workspace)
    try:
        generation = active_generation(db)
        check = check_case(db, workspace['blocks'], 'archive-01')
        assert check['ok'], check
        if generation is not None:
            assert verify_generation(db, workspace['blocks'], generation)['ok']
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['verification']['ok']
    assert result['release']['blob_after'] == 0
    db = connect_ws(workspace)
    try:
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
        assert read_source(db, 2, store_root=workspace['blocks']) == B_RAW
        assert check_case(db, workspace['blocks'], 'archive-01')['ok']
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        # Storage converges: no duplicate blocks and no orphan temp files.
        usage = BlockStore(workspace['blocks']).usage()
        assert usage['tmp_files'] == 0
        assert usage['block_files'] == 26
    finally:
        db.close()


def test_crash_after_blob_clear_before_finishing(workspace):
    """BLOB 已清、进程中断：the published generation is the only copy left."""
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser('blobs_cleared'))
    db = connect_ws(workspace)
    try:
        generation = active_generation(db)
        assert generation is not None
        assert verify_generation(db, workspace['blocks'], generation)['ok']
        assert db.execute('SELECT raw FROM source WHERE id=1').fetchone()[0] is None
        assert db.execute('SELECT storage FROM source WHERE id=1').fetchone()[0] == 'blocks'
        # The source identity and case are still fully verifiable from blocks.
        assert check_case(db, workspace['blocks'], 'archive-01')['ok']
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
        assert read_source(db, 2, store_root=workspace['blocks']) == B_RAW
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['release']['blob_after'] == 0
    assert result['gc']['removed_blocks'] == 0


def test_crash_after_pointer_switch_before_blob_release(workspace):
    """Pointer switched but BLOBs not yet cleared: both copies coexist."""
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser('published'))
    db = connect_ws(workspace)
    try:
        generation = active_generation(db)
        assert generation is not None
        assert verify_generation(db, workspace['blocks'], generation)['ok']
        # Legacy BLOBs still exist and match the manifests exactly.
        assert db.execute('SELECT raw FROM source WHERE id=1').fetchone()[0] == A_RAW
        assert db.execute('SELECT raw FROM source WHERE id=2').fetchone()[0] == B_RAW
        assert db.execute('SELECT storage FROM source WHERE id=1').fetchone()[0] == 'blob'
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['release']['blob_after'] == 0
    assert result['gc']['removed_blocks'] == 0


def test_crash_mid_block_write_leaves_no_partial_block(workspace):
    def hook(event, detail):
        if event == 'block_writing':
            raise Crash('mid-block-write')

    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'], hook=hook)
    store = BlockStore(workspace['blocks'])
    # A crash while staging leaves only a temp file; no truncated block file
    # can ever appear under a content address.
    assert store.usage()['tmp_files'] >= 1
    assert store.scan_block_hashes() == set()
    for block_sha in store.scan_block_hashes():
        assert sha256(store.path(block_sha).read_bytes()) == block_sha
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['verification']['ok']
    assert result['gc']['removed_tmp_files'] >= 1
    assert BlockStore(workspace['blocks']).usage()['tmp_files'] == 0
    db = connect_ws(workspace)
    try:
        assert check_case(db, workspace['blocks'], 'archive-01')['ok']
    finally:
        db.close()


def test_crash_after_blocks_but_before_manifest_publish_resumes(workspace):
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser('manifests_written'))
    db = connect_ws(workspace)
    try:
        assert active_generation(db) is None  # pointer not switched
        plan = db.execute(
            "SELECT * FROM archive_generation WHERE status='planned'").fetchone()
        assert plan is not None
        # Sources still have their original bytes; nothing was destroyed.
        assert db.execute('SELECT count(*) FROM source WHERE raw IS NOT NULL'
                          ).fetchone()[0] == 2
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['generation'] is not None
    assert result['verification']['ok']


def test_partial_manifest_rows_are_completed_not_duplicated(workspace):
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser('manifest_written'))
    db = connect_ws(workspace)
    try:
        assert db.execute('SELECT count(*) FROM source_manifest').fetchone()[0] == 0
        assert db.execute('SELECT count(*) FROM manifest_block').fetchone()[0] == 0
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['verification']['ok']
    db = connect_ws(workspace)
    try:
        assert db.execute('SELECT count(*) FROM source_manifest').fetchone()[0] == 2
        assert db.execute('SELECT count(*) FROM manifest_block').fetchone()[0] == 51
        assert db.execute('SELECT count(*) FROM generation_manifest'
                          ).fetchone()[0] == 2
    finally:
        db.close()


def test_gc_interleaved_with_old_reader_is_safe(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        reader = open_reader(db, workspace['blocks'])
        gen, expected = plan_generation(db, 'fixed_64')
        build(db, workspace['blocks'], gen)
        publish(db, gen, workspace['blocks'], expected_pointer=expected)
        # Even a live old reader keeps all its blocks through repeated GC.
        for _ in range(3):
            assert gc(db, workspace['blocks'])['removed_blocks'] == 0
        assert reader.read(1) == A_RAW
        reader.close()
        removed = gc(db, workspace['blocks'])
        assert removed['removed_blocks'] == 26
    finally:
        db.close()


def test_gc_dry_run_and_protected_planned_generation(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        gen, _ = plan_generation(db, 'fixed_64')
        build(db, workspace['blocks'], gen)
        # A built-but-unpublished (planned) generation is protected too.
        assert gc(db, workspace['blocks'])['removed_blocks'] == 0
        # Abandon it explicitly; its exclusive blocks become garbage.
        db.execute("UPDATE archive_generation SET status='abandoned' WHERE id=?", (gen,))
        db.commit()
        result = gc(db, workspace['blocks'])
        assert result['removed_blocks'] >= 84 - 26
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Legacy rollout
# ---------------------------------------------------------------------------

def _legacy_database(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE batch(id TEXT PRIMARY KEY,config_hash TEXT NOT NULL);
        CREATE TABLE subject(id TEXT PRIMARY KEY,batch_id TEXT NOT NULL REFERENCES batch(id));
        CREATE TABLE source(id INTEGER PRIMARY KEY,path TEXT NOT NULL,sha256 TEXT NOT NULL UNIQUE,raw BLOB NOT NULL);
        CREATE TABLE event(id INTEGER PRIMARY KEY,source_id INTEGER NOT NULL REFERENCES source(id),line_no INTEGER NOT NULL,identity TEXT NOT NULL UNIQUE,subject TEXT NOT NULL,kind TEXT NOT NULL,time TEXT NOT NULL,payload TEXT NOT NULL,sha256 TEXT NOT NULL,UNIQUE(source_id,line_no));
    """)
    db.execute("INSERT INTO meta VALUES ('config_hash','legacy')")
    db.execute("INSERT INTO batch VALUES ('batch-1','legacy')")
    db.execute("INSERT INTO subject VALUES ('B1','batch-1')")
    for source_id, (path, raw) in enumerate([(A_PATH, A_RAW), (B_PATH, B_RAW)], 1):
        db.execute('INSERT INTO source VALUES (?,?,?,?)',
                   (source_id, str(path), sha256(raw), raw))
        db.execute('INSERT INTO event(source_id,line_no,identity,subject,kind,time,payload,sha256) '
                   "VALUES (?,?,?,?,'gate','2026-01-01T08:05:00Z','{}',?)",
                   (source_id, 1, f'legacy-{source_id}', 'B1', 'h'))
    db.commit()
    db.close()


def test_legacy_database_upgrades_in_place(tmp_path):
    db_path = tmp_path / 'legacy.db'
    blocks = tmp_path / 'blocks'
    _legacy_database(db_path)
    db = connect(db_path)
    try:
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        assert [r[1] for r in db.execute('PRAGMA table_info(source)')] == [
            'id', 'path', 'sha256', 'raw', 'length', 'version',
            'created_generation', 'storage']
        assert db.execute('SELECT length FROM source WHERE id=1').fetchone()[0] == len(A_RAW)
    finally:
        db.close()
    assert register(db_path, 'legacy-case', FIX / 'archive-case.json')['sources'] == 2
    result = migrate(db_path, 'record_chunks', store_root=blocks)
    assert result['verification']['ok']
    db = connect(db_path)
    try:
        assert [tuple(r) for r in db.execute('SELECT source_id,identity FROM event ORDER BY id')] == [
            (1, 'legacy-1'), (2, 'legacy-2')]
        assert read_source(db, 1, store_root=blocks) == A_RAW
        assert check_case(db, blocks, 'legacy-case')['ok']
    finally:
        db.close()


def test_second_representation_builds_from_manifests_after_blob_release(workspace):
    first = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert first['release']['blob_after'] == 0
    db = connect_ws(workspace)
    try:
        assert db.execute('SELECT count(*) FROM source WHERE raw IS NOT NULL'
                          ).fetchone()[0] == 0
        src = db.execute('SELECT * FROM source WHERE id=1').fetchone()
        assert original_bytes(db, workspace['blocks'], src) == A_RAW
    finally:
        db.close()
    second = migrate(workspace['db'], 'fixed_64', store_root=workspace['blocks'])
    assert second['verification']['ok']
    db = connect_ws(workspace)
    try:
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
        assert read_source(db, 2, store_root=workspace['blocks']) == B_RAW
    finally:
        db.close()


def test_migrate_without_store_root_uses_sibling_directory(workspace):
    result = migrate(workspace['db'], 'record_chunks', release=False)
    root = archive.store_root_for(connect_ws(workspace))
    try:
        assert root.name.endswith('.blocks')
        assert BlockStore(root).usage()['block_files'] == 26
        db = connect_ws(workspace)
        try:
            assert read_source(db, 1) == A_RAW
        finally:
            db.close()
        assert result['verification']['ok']
    finally:
        shutil.rmtree(root, ignore_errors=True)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def test_cli_archive_verify_source_and_storage(workspace, capsys):
    from dockscan.__main__ import main
    import sys

    def run(*argv):
        old = sys.argv
        sys.argv = ['dockscan', *argv]
        try:
            main()
        finally:
            sys.argv = old
        return capsys.readouterr().out

    out = run('archive', '--db', str(workspace['db']), '--blocks', str(workspace['blocks']),
              '--representation', 'record_chunks')
    summary = json.loads(out)
    assert summary['verification_ok'] is True
    assert summary['release']['blob_after'] == 0
    assert summary['physical_after']['blob_bytes'] == 0

    out = run('verify', '--db', str(workspace['db']), '--blocks', str(workspace['blocks']))
    assert json.loads(out)['ok'] is True

    out_path = workspace['root'] / 'a.out'
    out = run('source', '--db', str(workspace['db']), '--blocks', str(workspace['blocks']),
              '--source-id', '1', '--out', str(out_path))
    assert json.loads(out)['bytes'] == len(A_RAW)
    assert out_path.read_bytes() == A_RAW

    out = run('storage', '--db', str(workspace['db']), '--blocks', str(workspace['blocks']))
    storage = json.loads(out)
    assert storage['generation']['unique_blocks'] == 26
    assert storage['physical']['blob_bytes'] == 0

    out = run('gc', '--db', str(workspace['db']), '--blocks', str(workspace['blocks']),
              '--dry-run')
    assert json.loads(out)['removed_blocks'] == 0


def test_cli_register_still_freezes_case(tmp_path):
    from dockscan.__main__ import main
    import sys
    db_path = tmp_path / 'ledger.db'
    freeze_config(db_path, FIX / 'config.json')
    import_file(db_path, A_PATH)
    import_file(db_path, B_PATH)
    old = sys.argv
    sys.argv = ['dockscan', 'register', '--db', str(db_path), '--case', 'cli-case',
                str(FIX / 'archive-case.json')]
    try:
        main()
    finally:
        sys.argv = old


# ---------------------------------------------------------------------------
# Whole-file dedupe has no effect on this fixture; block dedupe does
# ---------------------------------------------------------------------------

def test_whole_file_sha_dedupe_saves_nothing_but_block_dedupe_does(workspace):
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        data = accounting(db, result['generation'], workspace['blocks'])
    finally:
        db.close()
    dedupe = data['whole_file_sha_dedup']
    assert dedupe['sources'] == 2
    assert dedupe['unique_file_sha'] == 2
    assert dedupe['savable_bytes'] == 0
    physical = data['physical']
    # Blocks hold one copy of the 25 shared records, not one copy per source.
    assert physical['block_bytes'] == len(B_RAW)
    assert physical['block_bytes'] < len(A_RAW) + len(B_RAW)
    # The logical reference count is explicitly not the disk saving.
    assert data['manifest_block_refs'] == 51
    assert physical['block_bytes'] != data['manifest_block_refs']


def test_blob_release_numbers_are_before_and_after_physical(workspace):
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    release = result['release']
    assert release['blob_before'] == len(A_RAW) + len(B_RAW)
    assert release['blob_after'] == 0
    assert release['blob_logical_reclaimed'] == len(A_RAW) + len(B_RAW)
    # Physical before/after include the SQLite file, not just the BLOB payload.
    assert release['physical_before']['db_file_bytes'] > 0
    assert release['physical_after']['db_free_bytes'] == 0  # VACUUMed
    assert release['physical_after']['physical_bytes'] < release['physical_before']['physical_bytes']
    db = connect_ws(workspace)
    try:
        stored = json.loads(db.execute(
            "SELECT value FROM archive_state WHERE key='last_release'").fetchone()[0])
    finally:
        db.close()
    assert stored['blob_before'] == len(A_RAW) + len(B_RAW)
    assert stored['blob_after'] == 0


def test_resume_after_planned_generation_blocks_were_collected(workspace):
    """A crash leaves manifests; GC removes abandoned blocks; resume must rebuild."""
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser('manifests_written'))
    db = connect_ws(workspace)
    try:
        planned = db.execute(
            "SELECT id FROM archive_generation WHERE status='planned'").fetchone()['id']
        db.execute("UPDATE archive_generation SET status='abandoned' WHERE id=?", (planned,))
        db.commit()
        assert gc(db, workspace['blocks'])['removed_blocks'] == 26
        assert BlockStore(workspace['blocks']).scan_block_hashes() == set()
    finally:
        db.close()
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    assert result['verification']['ok']
    db = connect_ws(workspace)
    try:
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
        assert check_case(db, workspace['blocks'], 'archive-01')['ok']
    finally:
        db.close()


def test_stale_planned_manifest_cannot_publish_with_missing_blocks(workspace):
    with pytest.raises(Crash):
        migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                hook=raiser('manifests_written'))
    db = connect_ws(workspace)
    try:
        planned = db.execute(
            "SELECT id FROM archive_generation WHERE status='planned'").fetchone()['id']
        for block_sha in BlockStore(workspace['blocks']).scan_block_hashes():
            BlockStore(workspace['blocks']).path(block_sha).unlink()
        # Resuming must not trust the stale manifest rows.
        resumed, expected = plan_generation(db, 'record_chunks')
        assert resumed != planned
        build(db, workspace['blocks'], resumed)
        publish(db, resumed, workspace['blocks'], expected_pointer=expected)
        assert verify_generation(db, workspace['blocks'], resumed)['ok']
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
    finally:
        db.close()


def test_gc_uses_proof_of_reference_not_a_grace_period(workspace):
    """An open reader row protects blocks immediately; no sleep is involved."""
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        reader = open_reader(db, workspace['blocks'])
        gen, expected = plan_generation(db, 'fixed_64')
        build(db, workspace['blocks'], gen)
        publish(db, gen, workspace['blocks'], expected_pointer=expected)
        # The retired generation is protected purely because its reader row is
        # still open: the very first GC run keeps every shared-layout block.
        assert gc(db, workspace['blocks'])['removed_blocks'] == 0
        reader.close()
        # Closing the reader is the only event needed; the next GC may reclaim.
        assert gc(db, workspace['blocks'])['removed_blocks'] == 26
        assert reader.closed
    finally:
        db.close()


def test_reader_pin_is_recorded_before_first_byte(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        reader = open_reader(db, workspace['blocks'])
        row = db.execute('SELECT * FROM archive_reader WHERE id=?', (reader.reader_id,)).fetchone()
        assert row['generation'] == reader.generation_id
        assert row['closed_at'] is None
        other = connect_ws(workspace)
        try:
            gen, expected = plan_generation(other, 'fixed_64')
            build(other, workspace['blocks'], gen)
            publish(other, gen, workspace['blocks'], expected_pointer=expected)
            # The pinned read still resolves through the pinned generation.
            assert reader.read(2) == B_RAW
        finally:
            other.close()
        reader.close()
        row = db.execute('SELECT closed_at FROM archive_reader WHERE id=?',
                         (reader.reader_id,)).fetchone()
        assert row['closed_at'] is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Business decisions and evidence are identical under both representations
# ---------------------------------------------------------------------------

@pytest.mark.parametrize('representation', ['record_chunks', 'fixed_64'])
def test_decisions_evidence_and_report_identical_across_representations(tmp_path, representation):
    db_path = tmp_path / 'ledger.db'
    blocks = tmp_path / 'blocks'
    freeze_config(db_path, FIX / 'config.json')
    import_file(db_path, A_PATH)
    import_file(db_path, B_PATH)
    evaluate(db_path)
    db = connect(db_path)
    try:
        before = {
            'decision': [dict(r) for r in db.execute('SELECT * FROM decision ORDER BY 1')],
            'evidence': [dict(r) for r in db.execute('SELECT * FROM evidence ORDER BY 1,2')],
            'issue': [dict(r) for r in db.execute('SELECT * FROM issue ORDER BY 1,2')],
            'event': [dict(r) for r in db.execute('SELECT * FROM event ORDER BY id')],
            'source': [dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')],
        }
    finally:
        db.close()
    first = export(db_path, tmp_path / 'before.json')
    before_report = json.loads(first.read_text(encoding='utf-8'))
    result = migrate(db_path, representation, store_root=blocks)
    assert result['verification']['ok']
    db = connect(db_path)
    try:
        after = {
            'decision': [dict(r) for r in db.execute('SELECT * FROM decision ORDER BY 1')],
            'evidence': [dict(r) for r in db.execute('SELECT * FROM evidence ORDER BY 1,2')],
            'issue': [dict(r) for r in db.execute('SELECT * FROM issue ORDER BY 1,2')],
            'event': [dict(r) for r in db.execute('SELECT * FROM event ORDER BY id')],
            'source': [dict(r) for r in db.execute('SELECT id,path,sha256 FROM source ORDER BY id')],
        }
    finally:
        db.close()
    assert after == before
    after_report = json.loads(export(db_path, tmp_path / 'after.json').read_text(encoding='utf-8'))
    assert after_report['source'] == before_report['source']
    assert after_report['decision'] == before_report['decision']
    assert after_report['evidence'] == before_report['evidence']
    assert after_report['archive']['ok']
    assert after_report['archive']['accounting']['physical']['blob_bytes'] == 0
    # Byte-exact reconstruction of both distinct originals.
    db = connect(db_path)
    try:
        assert sha256(read_source(db, 1, store_root=blocks)) == A_SHA
        assert sha256(read_source(db, 2, store_root=blocks)) == B_SHA
        assert read_source(db, 1, store_root=blocks) != read_source(db, 2, store_root=blocks)
    finally:
        db.close()


def test_legacy_blob_and_manifest_coexist_during_migration(workspace):
    """Rollout state: both representations verify, pointer not yet switched."""
    result = migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'],
                     release=False, collect=False)
    assert result['release'] is None
    db = connect_ws(workspace)
    try:
        gen = active_generation(db)
        assert gen is not None
        assert db.execute('SELECT count(*) FROM source WHERE raw IS NOT NULL'
                          ).fetchone()[0] == 2
        verification = verify_generation(db, workspace['blocks'], gen)
        assert verification['ok']
        # Both copies are byte-identical for every source.
        for item in verification['sources']:
            assert item['reconstructed_sha256'] == item['sha256']
            raw = db.execute('SELECT raw FROM source WHERE id=?', (item['source_id'],)).fetchone()[0]
            assert sha256(raw) == item['sha256']
    finally:
        db.close()
    # Later release removes only the BLOBs.
    db = connect_ws(workspace)
    try:
        release = release_blobs(db, gen, workspace['blocks'], vacuum_after=True)
        assert release['blob_after'] == 0
        assert release['disk_reclaimed'] > 0
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
    finally:
        db.close()


def test_release_refuses_when_blocks_no_longer_verify(workspace):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'], release=False)
    db = connect_ws(workspace)
    try:
        gen = active_generation(db)
        manifest = db.execute(
            'SELECT * FROM source_manifest WHERE source_id=1').fetchone()
        block = db.execute(
            'SELECT * FROM manifest_block WHERE manifest_id=? ORDER BY ordinal',
            (manifest['id'],)).fetchone()
        BlockStore(workspace['blocks']).path(block['block_sha']).unlink()
        with pytest.raises(ValueError, match='archive_verification_failed'):
            release_blobs(db, gen, workspace['blocks'])
        # The untouched legacy BLOB is still the safe copy.
        assert blob_usage(db)['blob_bytes'] == len(A_RAW) + len(B_RAW)
    finally:
        db.close()


def test_publish_refuses_an_unverified_generation(workspace):
    db = connect_ws(workspace)
    try:
        gen, expected = plan_generation(db, 'record_chunks')
        build(db, workspace['blocks'], gen)
        manifest = db.execute(
            'SELECT * FROM source_manifest WHERE source_id=2').fetchone()
        for block in db.execute(
                'SELECT * FROM manifest_block WHERE manifest_id=?', (manifest['id'],)):
            BlockStore(workspace['blocks']).path(block['block_sha']).unlink()
        with pytest.raises(ValueError, match='archive_verification_failed'):
            publish(db, gen, workspace['blocks'], expected_pointer=expected)
        assert active_generation(db) is None
        # Legacy BLOBs untouched; the source is still fully readable.
        assert read_source(db, 1, store_root=workspace['blocks']) == A_RAW
    finally:
        db.close()


def test_gc_releases_pin_of_a_crashed_reader_process(workspace):
    """A dead PID must not pin blocks forever; liveness is checked, not slept on."""
    import subprocess
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    db = connect_ws(workspace)
    try:
        gen, expected = plan_generation(db, 'fixed_64')
        build(db, workspace['blocks'], gen)
        publish(db, gen, workspace['blocks'], expected_pointer=expected)
        # Simulate a reader opened by a process that then died.
        proc = subprocess.Popen(['true'])
        proc.wait()
        db.execute("INSERT INTO archive_reader(id,generation,pid,opened_at) "
                   "VALUES ('dead-reader',1,?,?)", (proc.pid, archive._now()))
        db.commit()
        result = gc(db, workspace['blocks'])
        assert result['closed_dead_readers'] == ['dead-reader']
        assert result['removed_blocks'] == 26
        row = db.execute("SELECT closed_at FROM archive_reader WHERE id='dead-reader'").fetchone()
        assert row['closed_at'] is not None
    finally:
        db.close()


def test_cli_case_check_and_gc(workspace, capsys):
    from dockscan.__main__ import main
    import sys
    db_path, blocks = workspace['db'], workspace['blocks']

    def run(*argv):
        old = sys.argv
        sys.argv = ['dockscan', *argv]
        try:
            main()
        finally:
            sys.argv = old
        return capsys.readouterr().out

    run('archive', '--db', str(db_path), '--blocks', str(blocks))
    result = json.loads(run('case-check', '--db', str(db_path), '--case', 'archive-01',
                            '--blocks', str(blocks)))
    assert result['ok']
    assert {s['sha256'] for s in result['sources']} == {A_SHA, B_SHA}
    out = json.loads(run('gc', '--db', str(db_path), '--blocks', str(blocks)))
    assert out['removed_blocks'] == 0


def test_new_import_after_archive_does_not_leave_a_duplicate_blob(workspace, tmp_path):
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    new_path = tmp_path / 'late2.jsonl'
    new_path.write_bytes(
        b'{"kind":"weight","subject":"B1","identity":"D1:98",'
        b'"time":"2026-01-01T08:07:00Z","dock":"D1","net_kg":20}\n')
    result = import_file(workspace['db'], new_path, store_root=workspace['blocks'])
    assert result['archived']['blobs_cleared'] == [result['source_id']]
    db = connect_ws(workspace)
    try:
        assert blob_usage(db)['blob_sources'] == 0
        assert db.execute('SELECT raw FROM source WHERE id=?',
                          (result['source_id'],)).fetchone()[0] is None
        generation = active_generation(db)
        assert verify_generation(db, workspace['blocks'], generation)['ok']
        assert read_source(db, result['source_id'],
                           store_root=workspace['blocks']) == new_path.read_bytes()
        assert check_case(db, workspace['blocks'], 'archive-01')['ok']
    finally:
        db.close()


def test_concurrent_new_imports_archive_and_publish_safely(workspace, tmp_path):
    """Many simultaneous imports must all get manifests in one consistent generation."""
    import threading
    migrate(workspace['db'], 'record_chunks', store_root=workspace['blocks'])
    paths = []
    for i in range(8):
        p = tmp_path / f'concurrent-{i}.jsonl'
        p.write_bytes(
            (f'{{"kind":"gate","subject":"B1","identity":"D1:{400 + i}",'
             f'"time":"2026-01-01T08:0{i}:00Z","dock":"D1"}}\n').encode())
        paths.append(p)
    errors = []

    def worker(path):
        try:
            import_file(workspace['db'], path, store_root=workspace['blocks'])
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(p,)) for p in paths]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    db = connect_ws(workspace)
    try:
        generation = active_generation(db)
        assert verify_generation(db, workspace['blocks'], generation)['ok']
        assert db.execute('SELECT count(*) FROM source').fetchone()[0] == 10
        assert db.execute('SELECT count(*) FROM source WHERE raw IS NOT NULL'
                          ).fetchone()[0] == 0
        assert db.execute('PRAGMA foreign_key_check').fetchall() == []
        for row in db.execute('SELECT id,sha256 FROM source'):
            assert sha256(read_source(db, row['id'], store_root=workspace['blocks'])) \
                == row['sha256']
        # Every source is reachable from the one published generation.
        assert db.execute(
            'SELECT count(*) FROM source s WHERE NOT EXISTS '
            '(SELECT 1 FROM generation_manifest gm WHERE gm.generation_id=? '
            'AND gm.source_id=s.id)', (generation,)).fetchone()[0] == 0
        assert gc(db, workspace['blocks'])['removed_blocks'] == 0
    finally:
        db.close()
