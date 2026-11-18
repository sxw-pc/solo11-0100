"""Content-addressed block archive for original source bytes.

Two lossless representations are supported for the same input:

``fixed_64``
    Split the original bytes every 64 bytes from offset zero.  Simple fixed
    boundaries, but inserting a record at the start of a file shifts every
    subsequent boundary, so almost nothing is shared between two such files.

``record_chunks``
    Split at JSONL record boundaries.  Each block is one raw record *including
    its actual delimiter bytes*; a leading BOM and any trailing unterminated
    bytes are kept beside the block list.  Records that are unchanged between
    files share physical blocks even when one file has an extra record at the
    front.

Blocks live under ``<root>/blocks/<sha[:2]>/<sha>`` and are content-addressed
and immutable.  Manifests live in SQLite and pin the exact ordered block list,
length, SHA-256, prefix/suffix and representation.  Source ids, paths, hashes
and versions never change during migration, so ``event.source_id``,
``evidence`` and report references remain valid.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .store import connect

BOM = b'\xef\xbb\xbf'
CHUNK = 64
REPRESENTATIONS = ('fixed_64', 'record_chunks')
POINTER_KEY = 'archive_generation'
STORE_ROOT_KEY = 'store_root'


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def split_records(raw: bytes):
    """Return ``(prefix, blocks, suffix)`` with every input byte accounted for.

    A block is a record plus the exact delimiter bytes that followed it
    (``\\n`` or ``\\r\\n``).  The leading BOM is the prefix; bytes after the
    final delimiter (an unterminated last record) are the suffix.  A file that
    ends with a newline therefore has an empty suffix, which is how the
    "trailing newline present/absent" state is preserved.
    """
    prefix = BOM if raw.startswith(BOM) else b''
    body = raw[len(prefix):]
    blocks = []
    i = 0
    while True:
        nl = body.find(b'\n', i)
        if nl < 0:
            break
        blocks.append(body[i:nl + 1])
        i = nl + 1
    return prefix, blocks, body[i:]


def split_fixed(raw: bytes, block_bytes: int = CHUNK):
    if block_bytes <= 0:
        raise ValueError('bad_block_bytes')
    blocks = [raw[i:i + block_bytes] for i in range(0, len(raw), block_bytes)]
    return b'', blocks, b''


def chunk(raw: bytes, representation: str, block_bytes: int = CHUNK):
    if representation == 'fixed_64':
        return split_fixed(raw, block_bytes)
    if representation == 'record_chunks':
        return split_records(raw)
    raise ValueError('unknown_representation')


# ---------------------------------------------------------------------------
# Block store
# ---------------------------------------------------------------------------

class BlockStore:
    def __init__(self, root):
        self.root = Path(root)
        self.blocks = self.root / 'blocks'
        self.tmp = self.root / 'tmp'

    def path(self, block_sha: str) -> Path:
        return self.blocks / block_sha[:2] / block_sha

    def ensure(self) -> None:
        self.blocks.mkdir(parents=True, exist_ok=True)
        self.tmp.mkdir(parents=True, exist_ok=True)

    def has(self, block_sha: str) -> bool:
        return self.path(block_sha).is_file()

    def put(self, data: bytes, hook=None, partial_hook=None) -> str:
        """Store ``data`` once.  Returns its content address.

        The write is staged in ``tmp`` and linked/renamed into place, so an
        interrupted write can never leave a truncated content-addressed block.
        ``partial_hook`` runs after half of the bytes hit the temporary file;
        ``hook('block_staged', ...)`` runs once the complete temporary file
        exists but before it becomes visible under its content address.  Both
        simulate crash points and leave only an ignorable ``.part`` file.
        """
        digest = sha256(data)
        target = self.path(digest)
        if target.is_file():
            return digest
        self.ensure()
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.tmp / f'{digest}.{os.getpid()}.{uuid.uuid4().hex}.part'
        with open(tmp, 'wb') as fh:
            if partial_hook:
                half = len(data) // 2
                fh.write(data[:half])
                fh.flush()
                os.fsync(fh.fileno())
                partial_hook('block_partial', {'tmp': str(tmp), 'block_sha': digest,
                                               'written': half, 'total': len(data)})
                fh.write(data[half:])
            else:
                fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        if hook:
            hook('block_staged', {'tmp': str(tmp), 'block_sha': digest, 'length': len(data)})
        try:
            try:
                os.link(tmp, target)
            except FileExistsError:
                pass
            except OSError:
                # Filesystems without hard links: rename is still atomic.
                if not target.exists():
                    os.replace(tmp, target)
                    tmp = None
        finally:
            if tmp is not None:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
        return digest

    def get(self, block_sha: str) -> bytes:
        return self.path(block_sha).read_bytes()

    def usage(self) -> dict:
        total = 0
        count = 0
        if self.blocks.is_dir():
            for p in self.blocks.rglob('*'):
                if p.is_file():
                    total += p.stat().st_size
                    count += 1
        tmp_bytes = 0
        tmp_files = 0
        if self.tmp.is_dir():
            for p in self.tmp.rglob('*'):
                if p.is_file():
                    tmp_bytes += p.stat().st_size
                    tmp_files += 1
        return {'block_files': count, 'block_bytes': total,
                'tmp_files': tmp_files, 'tmp_bytes': tmp_bytes}

    def scan_block_hashes(self):
        found = set()
        if not self.blocks.is_dir():
            return found
        for p in self.blocks.rglob('*'):
            if p.is_file() and len(p.name) == 64:
                found.add(p.name)
        return found


# ---------------------------------------------------------------------------
# Storage roots and small helpers
# ---------------------------------------------------------------------------

def _now():
    return time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())


@contextmanager
def _write(db):
    """Serialise writers with ``BEGIN IMMEDIATE`` and commit/rollback cleanly.

    Re-entrant: a nested call joins the caller's transaction instead of
    committing it out from under the caller.
    """
    if db.in_transaction:
        yield
        return
    db.execute('BEGIN IMMEDIATE')
    try:
        yield
    except BaseException:
        try:
            db.execute('ROLLBACK')
        except Exception:
            pass
        raise
    else:
        db.execute('COMMIT')


def _database_path(db):
    row = db.execute('PRAGMA database_list').fetchone()
    return row['file'] if row else ''


def default_store_root(db):
    path = _database_path(db)
    if path:
        return Path(str(path) + '.blocks')
    return Path(tempfile.mkdtemp(prefix='dockscan-blocks-'))


def store_root_for(db, store_root=None, persist=False):
    """Resolve the block store root, recording an explicit choice.

    An explicit root is persisted in ``archive_state`` so later readers, GC and
    reports use the same blocks.  Without one, an already recorded root wins;
    otherwise a sibling ``<db>.blocks`` directory is used.
    """
    if store_root is not None:
        root = Path(store_root)
        set_store_root(db, root)
        return root
    row = db.execute('SELECT value FROM archive_state WHERE key=?', (STORE_ROOT_KEY,)).fetchone()
    if row:
        return Path(row['value'])
    root = default_store_root(db)
    if persist:
        set_store_root(db, root)
    return root


def set_store_root(db, root):
    root = Path(root)
    with _write(db):
        db.execute('INSERT INTO archive_state(key,value) VALUES (?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (STORE_ROOT_KEY, str(root)))
    return root


# ---------------------------------------------------------------------------
# Generations and manifests
# ---------------------------------------------------------------------------

def _normalise_representation(name):
    if name not in REPRESENTATIONS:
        raise ValueError('unknown_representation')
    return name


def active_generation(db):
    row = db.execute('SELECT value FROM archive_state WHERE key=?', (POINTER_KEY,)).fetchone()
    return int(row['value']) if row and row['value'] else None


def _generation(db, generation_id):
    row = db.execute('SELECT * FROM archive_generation WHERE id=?', (generation_id,)).fetchone()
    if row is None:
        raise ValueError('generation_missing')
    return row


def _missing_manifests(db, generation_id):
    return db.execute(
        'SELECT count(*) FROM source s WHERE NOT EXISTS '
        '(SELECT 1 FROM generation_manifest gm WHERE gm.generation_id=? AND gm.source_id=s.id)',
        (generation_id,)).fetchone()[0]


def _generation_blocks_present(db, block_store, generation_id):
    for row in db.execute(
            'SELECT DISTINCT b.block_sha FROM manifest_block b '
            'JOIN generation_manifest gm ON gm.manifest_id=b.manifest_id '
            'WHERE gm.generation_id=?', (generation_id,)):
        if not block_store.has(row[0]):
            return False
    return True


def _manifest(db, source_id, generation_id):
    return db.execute(
        'SELECT * FROM source_manifest WHERE source_id=? AND generation_id=?',
        (source_id, generation_id)).fetchone()


def _manifest_blocks(db, manifest_id):
    return db.execute(
        'SELECT ordinal,block_sha,length FROM manifest_block WHERE manifest_id=? ORDER BY ordinal',
        (manifest_id,)).fetchall()


def original_bytes(db, root, src):
    """Original bytes for a source, from its BLOB or an existing manifest.

    After a first migration has released the legacy BLOBs, a second
    representation is built by materializing the already-published manifests.
    The value is always checked against the frozen ``source.sha256``.
    """
    if src['raw'] is not None:
        raw = bytes(src['raw'])
    else:
        row = db.execute(
            'SELECT m.* FROM source_manifest m JOIN archive_generation g ON g.id=m.generation_id '
            "WHERE m.source_id=? AND g.status IN ('published','planned') "
            'ORDER BY m.id DESC LIMIT 1', (src['id'],)).fetchone()
        if row is None:
            raise ValueError('source_unavailable')
        raw = _materialize_manifest(db, root, row)
    if sha256(raw) != src['sha256']:
        raise ValueError('archive_source_hash_mismatch')
    return raw


def _insert_manifest_locked(db, root, gen, src, hook=None):
    """Write one source's blocks and manifest inside the caller's transaction."""
    raw = original_bytes(db, root, src)
    if sha256(raw) != src['sha256']:
        raise ValueError('archive_source_hash_mismatch')
    representation = gen['representation']
    block_bytes = gen['block_bytes'] if representation == 'fixed_64' else CHUNK
    prefix, blocks, suffix = chunk(raw, representation, block_bytes)
    block_store = BlockStore(root)
    entries = []
    for ordinal, data in enumerate(blocks):
        if hook:
            # Mid-block crash: only half of this block reaches the temporary
            # file, so no content-addressed block file is ever created.
            def staged(event, detail, _ordinal=ordinal, _data=data):
                if event == 'block_partial':
                    hook('block_writing', {
                        'source_id': src['id'], 'ordinal': _ordinal,
                        'block_sha': sha256(_data), 'bytes_total': len(_data)})
                hook(event, detail)
            digest = block_store.put(data, hook=staged, partial_hook=staged)
        else:
            digest = block_store.put(data)
        entries.append({'ordinal': ordinal, 'sha256': digest, 'length': len(data)})
        if hook:
            hook('block_written', {
                'source_id': src['id'], 'ordinal': ordinal, 'block_sha': digest,
                'blocks_done': ordinal + 1, 'blocks_total': len(blocks),
            })
    payload = json.dumps({
        'source_id': src['id'],
        'sha256': src['sha256'],
        'length': len(raw),
        'version': int(src['version']),
        'representation': representation,
        'block_bytes': block_bytes,
        'prefix': prefix.hex(),
        'suffix': suffix.hex(),
        'blocks': entries,
    }, sort_keys=True, separators=(',', ':'))
    manifest_id = db.execute(
        'INSERT INTO source_manifest(source_id,generation_id,version,representation,'
        'block_bytes,length,sha256,prefix,suffix,block_count,payload,status,created_at) '
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'ready',?)",
        (src['id'], gen['id'], int(src['version']), representation, block_bytes,
         len(raw), src['sha256'], prefix, suffix, len(entries), payload, _now())).lastrowid
    db.executemany(
        'INSERT INTO manifest_block(manifest_id,ordinal,block_sha,length) VALUES (?,?,?,?)',
        [(manifest_id, e['ordinal'], e['sha256'], e['length']) for e in entries])
    db.execute('INSERT INTO generation_manifest(generation_id,source_id,manifest_id) '
               'VALUES (?,?,?)', (gen['id'], src['id'], manifest_id))
    if hook:
        hook('manifest_written', {
            'source_id': src['id'], 'manifest_id': manifest_id, 'block_count': len(entries),
        })
    return manifest_id, entries


def plan_generation(db, representation, block_bytes=CHUNK):
    """Return a build target and the pointer value expected at publish time.

    Resumption rules:

    * an already published generation with the same representation is reused
      (migration crashed after the pointer switch or after blob cleanup);
    * otherwise an existing *planned* generation with the same representation
      is resumed;
    * otherwise a new planned generation is created and any older abandoned
      plan is marked ``abandoned`` (its blocks become GC candidates).
    """
    representation = _normalise_representation(representation)
    if representation == 'fixed_64':
        block_bytes = int(block_bytes)
        if block_bytes <= 0:
            raise ValueError('bad_block_bytes')
    else:
        block_bytes = CHUNK
    block_store = BlockStore(store_root_for(db))
    with _write(db):
        expected = active_generation(db)
        # A planned generation is only resumable if every block its manifests
        # already committed is still physically present.  After a crash + GC,
        # resuming would otherwise publish a manifest pointing at missing
        # blocks, so start a fresh generation and rebuild all of them.
        for row in db.execute(
                "SELECT id FROM archive_generation WHERE status='planned' AND representation=? "
                'AND block_bytes=? ORDER BY id DESC', (representation, block_bytes)).fetchall():
            if _generation_blocks_present(db, block_store, row['id']):
                return int(row['id']), expected
            db.execute("UPDATE archive_generation SET status='abandoned',retired_at=? WHERE id=?",
                       (_now(), row['id']))
        if expected is not None:
            gen = _generation(db, expected)
            if gen['representation'] == representation and _missing_manifests(db, expected) == 0:
                return expected, expected
            if gen['representation'] == representation and gen['status'] == 'published':
                # A source was added without a manifest in the active
                # generation; a fresh generation will include it.
                pass
        db.execute(
            "UPDATE archive_generation SET status='abandoned', retired_at=? "
            "WHERE status='planned'", (_now(),))
        sources = [r['id'] for r in db.execute('SELECT id FROM source ORDER BY id')]
        gen_id = db.execute(
            'INSERT INTO archive_generation(status,representation,block_bytes,plan,created_at) '
            "VALUES ('planned',?,?,?,?)",
            (representation, block_bytes,
             json.dumps({'sources': sources, 'expected_pointer': expected}),
             _now())).lastrowid
    return int(gen_id), expected


def build(db, store_root, generation_id, hook=None):
    """Write all missing blocks/manifests for a planned generation.

    Blocks are content-addressed, so re-running after a crash is idempotent.
    The database changes are one transaction; a hook exception rolls back the
    manifests but leaves already written (complete) block files in place for a
    later retry or GC.
    """
    block_store = BlockStore(store_root)
    block_store.ensure()
    gen = _generation(db, generation_id)
    if gen['status'] in ('published', 'retired'):
        if _missing_manifests(db, generation_id):
            raise ValueError('generation_incomplete')
        return {'generation': generation_id, 'status': gen['status'],
                'manifests': 0, 'blocks_written': 0}
    if gen['status'] != 'planned':
        raise ValueError('generation_not_buildable')
    written = 0
    manifests = 0
    with _write(db):
        sources = db.execute('SELECT * FROM source ORDER BY id').fetchall()
        for src in sources:
            if _manifest(db, src['id'], generation_id) is not None:
                continue
            manifest_id, entries = _insert_manifest_locked(db, store_root, gen, dict(src), hook=hook)
            written += len(entries)
            manifests += 1
        missing = _missing_manifests(db, generation_id)
        if missing:
            raise ValueError('generation_incomplete')
    if hook:
        hook('manifests_written', {'generation': generation_id, 'manifests': manifests,
                                   'blocks_written': written})
    return {'generation': generation_id, 'status': 'built',
            'manifests': manifests, 'blocks_written': written}


def _materialize_manifest(db, store_root, manifest):
    blocks = _manifest_blocks(db, manifest['id'])
    if len(blocks) != manifest['block_count']:
        raise ValueError('manifest_block_count_mismatch')
    block_store = BlockStore(store_root)
    data = bytearray(manifest['prefix'] or b'')
    for row in blocks:
        piece = block_store.get(row['block_sha'])
        if len(piece) != row['length'] or sha256(piece) != row['block_sha']:
            raise ValueError('block_corrupt')
        data += piece
    data += manifest['suffix'] or b''
    if len(data) != manifest['length']:
        raise ValueError('manifest_length_mismatch')
    if sha256(bytes(data)) != manifest['sha256']:
        raise ValueError('manifest_sha256_mismatch')
    return bytes(data)


def materialize(db, store_root, manifest=None, manifest_id=None):
    if manifest is None:
        manifest = db.execute('SELECT * FROM source_manifest WHERE id=?', (manifest_id,)).fetchone()
        if manifest is None:
            raise ValueError('manifest_missing')
    return _materialize_manifest(db, store_root, manifest)


def verify_generation(db, store_root, generation_id):
    """Reconstruct every manifest and compare with the frozen legacy bytes."""
    result = {'generation': generation_id, 'sources': [], 'ok': True}
    rows = db.execute(
        'SELECT m.*,s.raw,s.path AS source_path,s.version AS source_version '
        'FROM source_manifest m JOIN source s ON s.id=m.source_id '
        'WHERE m.generation_id=? ORDER BY m.source_id', (generation_id,)).fetchall()
    block_store = BlockStore(store_root)
    for row in rows:
        item = {'source_id': row['source_id'], 'manifest_id': row['id'],
                'path': row['source_path'], 'sha256': row['sha256'], 'length': row['length'],
                'version': row['source_version'], 'representation': row['representation'],
                'block_count': row['block_count'], 'manifest_version': row['version'],
                'ok': True, 'errors': []}
        try:
            data = _materialize_manifest(db, store_root, row)
        except Exception as exc:
            item['ok'] = False
            item['errors'].append(f'materialize:{exc}')
            data = None
        if data is not None:
            if len(data) != row['length']:
                item['ok'] = False
                item['errors'].append('length_mismatch')
            if sha256(data) != row['sha256']:
                item['ok'] = False
                item['errors'].append('sha256_mismatch')
            if row['raw'] is not None and bytes(row['raw']) != data:
                item['ok'] = False
                item['errors'].append('legacy_blob_mismatch')
            item['reconstructed_sha256'] = sha256(data)
        result['ok'] = result['ok'] and item['ok']
        result['sources'].append(item)
    result['missing_manifests'] = _missing_manifests(db, generation_id)
    if result['missing_manifests']:
        result['ok'] = False
    result['block_usage'] = block_store.usage()
    return result


def publish(db, generation_id, store_root=None, verify=True, expected_pointer=None):
    """Compare-and-swap the active generation pointer.

    The pointer changes only after all manifests exist and every source
    reconstructs to its frozen SHA.  ``expected_pointer`` turns the switch into
    a CAS: if another writer published in the meantime this raises
    ``generation_pointer_conflict`` and the caller replans.
    """
    gen = _generation(db, generation_id)
    if gen['status'] in ('abandoned',):
        raise ValueError('generation_not_publishable')
    if verify:
        if store_root is None:
            store_root = store_root_for(db)
        check = verify_generation(db, store_root, generation_id)
        if not check['ok']:
            raise ValueError('archive_verification_failed')
    if store_root is None:
        store_root = store_root_for(db)
    with _write(db):
        current = active_generation(db)
        if expected_pointer is not None and current != expected_pointer:
            raise ValueError('generation_pointer_conflict')
        if current == generation_id:
            return {'generation': generation_id, 'previous': current, 'switched': False}
        missing = _missing_manifests(db, generation_id)
        if missing:
            raise ValueError('generation_incomplete')
        if current is not None:
            db.execute("UPDATE archive_generation SET status='retired',retired_at=? WHERE id=?",
                       (_now(), current))
        db.execute("UPDATE archive_generation SET status='published',published_at=? WHERE id=?",
                   (_now(), generation_id))
        db.execute(
            'INSERT INTO archive_state(key,value) VALUES (?,?) '
            'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
            (POINTER_KEY, str(generation_id)))
    if verify:
        # Re-check only the newly active pointer's identity.
        assert active_generation(db) == generation_id
    return {'generation': generation_id, 'previous': current, 'switched': True}


def publish_generation(db_path, generation_id, store_root=None, expected_pointer=None):
    db = connect(db_path)
    try:
        return publish(db, generation_id, store_root, expected_pointer=expected_pointer)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Readers pinned to one generation
# ---------------------------------------------------------------------------

class Reader:
    def __init__(self, db, store_root, generation_id, reader_id):
        self.db = db
        self.store_root = Path(store_root)
        self.generation_id = generation_id
        self.reader_id = reader_id
        self._closed = False

    @property
    def closed(self):
        return self._closed

    def manifest_for(self, source_id):
        row = self.db.execute(
            'SELECT manifest_id FROM generation_manifest WHERE generation_id=? AND source_id=?',
            (self.generation_id, source_id)).fetchone()
        if row is None:
            return None
        return self.db.execute('SELECT * FROM source_manifest WHERE id=?',
                               (row['manifest_id'],)).fetchone()

    def read(self, source_id):
        manifest = self.manifest_for(source_id)
        if manifest is None:
            return None
        return _materialize_manifest(self.db, self.store_root, manifest)

    def read_manifest(self, manifest_id):
        return materialize(self.db, self.store_root, manifest_id=manifest_id)

    def close(self):
        if self._closed:
            return
        self._closed = True
        try:
            with _write(self.db):
                self.db.execute('UPDATE archive_reader SET closed_at=? WHERE id=?',
                                (_now(), self.reader_id))
        except sqlite3.ProgrammingError:
            # The owning connection was already closed; a later GC run with a
            # fresh connection cannot observe this reader as active anyway.
            pass


def open_reader(db, store_root=None, generation_id=None):
    """Pin one complete generation for the whole read.

    The generation id and store root are recorded before the first byte is
    read, so a concurrent pointer switch cannot make a single read observe two
    generations.  Returns ``None`` when no archive exists yet (rollout state);
    callers then fall back to legacy BLOBs.
    """
    if generation_id is None:
        generation_id = active_generation(db)
    if generation_id is None:
        return None
    gen = db.execute('SELECT * FROM archive_generation WHERE id=?', (generation_id,)).fetchone()
    if gen is None:
        raise ValueError('generation_missing')
    if gen['status'] == 'abandoned':
        raise ValueError('generation_not_pinnable')
    if store_root is None:
        store_root = store_root_for(db)
    reader_id = f'{os.getpid()}-{uuid.uuid4().hex}'
    with _write(db):
        db.execute('INSERT INTO archive_reader(id,generation,pid,opened_at) VALUES (?,?,?,?)',
                   (reader_id, generation_id, os.getpid(), _now()))
    return Reader(db, store_root, generation_id, reader_id)


def read_source(db, source_id, store_root=None, generation_id=None, reader=None):
    """Return the original bytes for ``source_id``.

    A pinned manifest is preferred.  During rollout, or for a source that only
    exists as a legacy BLOB, the BLOB is returned after its SHA is checked.
    """
    own_reader = False
    if reader is None:
        if store_root is None:
            store_root = store_root_for(db)
        reader = open_reader(db, store_root, generation_id)
        own_reader = True
    try:
        if reader is not None:
            data = reader.read(source_id)
            if data is not None:
                return data
        row = db.execute('SELECT raw,sha256 FROM source WHERE id=?', (source_id,)).fetchone()
        if row is None:
            raise ValueError('source_missing')
        if row['raw'] is None:
            raise ValueError('source_unavailable')
        data = bytes(row['raw'])
        if sha256(data) != row['sha256']:
            raise ValueError('source_blob_corrupt')
        return data
    finally:
        if own_reader and reader is not None:
            reader.close()


def read_source_by_path(db, path, **kwargs):
    row = db.execute('SELECT id FROM source WHERE path=? ORDER BY id LIMIT 1', (str(path),)).fetchone()
    if row is None:
        raise ValueError('source_missing')
    return read_source(db, row['id'], **kwargs)


# ---------------------------------------------------------------------------
# Blob release and garbage collection
# ---------------------------------------------------------------------------

def blob_usage(db):
    row = db.execute('SELECT count(*) AS files, COALESCE(sum(length(raw)),0) AS bytes '
                     'FROM source WHERE raw IS NOT NULL').fetchone()
    return {'blob_sources': row['files'], 'blob_bytes': row['bytes']}


def _db_file_bytes(db):
    path = _database_path(db)
    if not path:
        return {'db_file_bytes': 0, 'db_wal_bytes': 0, 'db_shm_bytes': 0}
    base = Path(path)
    def size(p):
        try:
            return p.stat().st_size
        except FileNotFoundError:
            return 0
    return {'db_file_bytes': size(base),
            'db_wal_bytes': size(Path(str(base) + '-wal')),
            'db_shm_bytes': size(Path(str(base) + '-shm'))}


def physical_usage(db, store_root=None):
    """Honest physical accounting: block files, DB files, and logical BLOBs.

    ``blob_bytes`` is the logical BLOB payload still stored inside the SQLite
    file; it is *not* added to ``physical_bytes`` because that would double
    count the database file.  ``db_free_bytes`` shows space SQLite has not
    returned to the filesystem yet (it shrinks only after VACUUM).
    """
    if store_root is None:
        store_root = store_root_for(db)
    blocks = BlockStore(store_root).usage()
    files = _db_file_bytes(db)
    blobs = blob_usage(db)
    try:
        page_size = db.execute('PRAGMA page_size').fetchone()[0]
        free_pages = db.execute('PRAGMA freelist_count').fetchone()[0]
        db_free = page_size * free_pages
    except Exception:
        db_free = 0
    return {
        'block_files': blocks['block_files'],
        'block_bytes': blocks['block_bytes'],
        'tmp_files': blocks['tmp_files'],
        'tmp_bytes': blocks['tmp_bytes'],
        **files,
        'db_free_bytes': db_free,
        'blob_sources': blobs['blob_sources'],
        'blob_bytes': blobs['blob_bytes'],
        'physical_bytes': blocks['block_bytes'] + files['db_file_bytes']
                          + files['db_wal_bytes'] + blocks['tmp_bytes'],
    }


def _checkpoint(db):
    try:
        db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    except Exception:
        pass


def vacuum(db):
    """Return freed SQLite pages to the filesystem (best effort)."""
    db.commit()
    try:
        db.execute('VACUUM')
        _checkpoint(db)
        return True
    except Exception:
        return False


def release_blobs(db, generation_id=None, store_root=None, verify=True, vacuum_after=False,
                  hook=None):
    """Clear legacy ``source.raw`` BLOBs whose manifest has been verified.

    Source rows, ids, paths, hashes, lengths and versions remain untouched, so
    events, evidence and reports keep pointing at the same source identity.
    """
    if generation_id is None:
        generation_id = active_generation(db)
    if generation_id is None:
        raise ValueError('no_active_generation')
    gen = _generation(db, generation_id)
    if gen['status'] not in ('published', 'retired'):
        raise ValueError('generation_not_published')
    if store_root is None:
        store_root = store_root_for(db)
    if verify:
        check = verify_generation(db, store_root, generation_id)
        if not check['ok']:
            raise ValueError('archive_verification_failed')
    _checkpoint(db)
    before = physical_usage(db, store_root)
    cleared = []
    with _write(db):
        rows = db.execute(
            'SELECT s.id FROM source s JOIN generation_manifest gm '
            'ON gm.source_id=s.id AND gm.generation_id=? WHERE s.raw IS NOT NULL '
            'ORDER BY s.id', (generation_id,)).fetchall()
        for row in rows:
            db.execute("UPDATE source SET raw=NULL,storage='blocks' WHERE id=?", (row['id'],))
            cleared.append(row['id'])
    if hook:
        hook('blobs_cleared', {'generation': generation_id, 'cleared': list(cleared)})
    if verify:
        # Every source in the generation must still reconstruct after the
        # transaction, otherwise the pointer/release is unsafe.
        check = verify_generation(db, store_root, generation_id)
        if not check['ok']:
            raise ValueError('post_release_verification_failed')
    if vacuum_after:
        vacuum(db)
    _checkpoint(db)
    after = physical_usage(db, store_root)
    result = {
        'generation': generation_id,
        'cleared_sources': cleared,
        'blob_before': before['blob_bytes'],
        'blob_after': after['blob_bytes'],
        'blob_logical_reclaimed': before['blob_bytes'] - after['blob_bytes'],
        'physical_before': before,
        'physical_after': after,
        'disk_reclaimed': before['physical_bytes'] - after['physical_bytes'],
        'vacuumed': bool(vacuum_after),
    }
    summary = {k: result[k] for k in ('generation', 'blob_before', 'blob_after',
                                      'blob_logical_reclaimed', 'physical_before',
                                      'physical_after', 'disk_reclaimed')}
    with _write(db):
        db.execute('INSERT INTO archive_state(key,value) VALUES (?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   ('last_release', json.dumps(summary, sort_keys=True)))
    if hook:
        hook('blobs_released', result)
    return result


def _protected_generations(db):
    """Generations whose blocks must be kept right now.

    * the published generation and any in-progress (planned) build;
    * every generation pinned by an unclosed reader, including retired ones.

    A retired generation with no reader is no longer reachable through
    ``open_reader`` and its exclusive blocks are garbage.
    """
    generations = {row[0] for row in db.execute(
        "SELECT id FROM archive_generation WHERE status IN ('published','planned')")}
    generations |= {row[0] for row in db.execute(
        'SELECT DISTINCT generation FROM archive_reader WHERE closed_at IS NULL')}
    return generations


def referenced_block_hashes(db):
    return {row[0] for row in db.execute(
        'SELECT DISTINCT b.block_sha FROM manifest_block b '
        'JOIN generation_manifest gm ON gm.manifest_id=b.manifest_id '
        'JOIN archive_generation g ON g.id=gm.generation_id '
        "WHERE g.status IN ('published','planned')")}


def _reader_is_live(db, reader_id):
    row = db.execute('SELECT pid FROM archive_reader WHERE id=?', (reader_id,)).fetchone()
    if row is None or row['pid'] == os.getpid():
        return True
    try:
        os.kill(row['pid'], 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def close_dead_readers(db):
    """Mark reader rows whose process no longer exists as closed.

    GC safety is proven by rows, not by waiting.  A crashed reader process can
    never read again, so its pin is released explicitly here instead of being
    leaked until a timeout.
    """
    closed = []
    with _write(db):
        for row in db.execute(
                'SELECT id,pid FROM archive_reader WHERE closed_at IS NULL').fetchall():
            if row['pid'] != os.getpid() and not _reader_is_live(db, row['id']):
                db.execute('UPDATE archive_reader SET closed_at=? WHERE id=?',
                           (_now(), row['id']))
                closed.append(row['id'])
    return closed


def gc(db, store_root=None, dry_run=False, hook=None, grace_seconds=None):
    """Delete blocks not referenced by a live generation or an open reader.

    The reference set and the reader set are read while holding the writer
    lock, so a concurrent build/import cannot slip a block in between the
    check and the deletion.  No sleeps or grace timers are used.  Reader rows
    belonging to dead processes are closed first, so one crashed reader cannot
    pin blocks forever.

    ``grace_seconds`` is an optional *additional* lease for blocks that are
    referenced only by manifests of abandoned/stale generations (e.g. a
    generation whose block files were orphaned by a crash).  It never replaces
    reference proof: live generations and open readers protect regardless, and
    unreachable blocks whose files are older than the lease are reclaimed.
    Passing ``None`` (the default) means "delete all unreferenced blocks now";
    no sleep is ever used internally.
    """
    if store_root is None:
        store_root = store_root_for(db)
    block_store = BlockStore(store_root)
    removed = []
    removed_bytes = 0
    dead_readers = []
    if not dry_run:
        dead_readers = close_dead_readers(db)
    with _write(db):
        protected_gens = _protected_generations(db)
        if protected_gens:
            placeholders = ','.join('?' * len(protected_gens))
            # Protection follows generation_manifest, not
            # source_manifest.generation_id: a newer generation may reuse an
            # older generation's manifest row, and those blocks are still live.
            protected = {row[0] for row in db.execute(
                'SELECT DISTINCT b.block_sha FROM manifest_block b '
                'JOIN generation_manifest gm ON gm.manifest_id=b.manifest_id '
                f'WHERE gm.generation_id IN ({placeholders})',
                tuple(sorted(protected_gens)))}
        else:
            protected = set()
        on_disk = block_store.scan_block_hashes()
        unused = sorted(on_disk - protected)
        if grace_seconds is not None:
            cutoff = time.time() - float(grace_seconds)
            unused = [digest for digest in unused
                      if block_store.path(digest).stat().st_mtime <= cutoff]
        tmp_files = []
        tmp_bytes = 0
        if block_store.tmp.is_dir():
            for p in block_store.tmp.rglob('*'):
                if p.is_file():
                    try:
                        tmp_bytes += p.stat().st_size
                    except FileNotFoundError:
                        pass
                    tmp_files.append(p)
        if hook:
            hook('gc_scan', {'protected': len(protected), 'on_disk': len(on_disk),
                             'unused': len(unused), 'generations': sorted(protected_gens)})
        for digest in unused:
            path = block_store.path(digest)
            try:
                size = path.stat().st_size
            except FileNotFoundError:
                size = 0
            if not dry_run:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
            removed.append(digest)
            removed_bytes += size
        if not dry_run:
            # Holding the writer lock means no builder is currently staging a
            # block, so every ``.part`` file is an artifact of a crashed write.
            for p in tmp_files:
                try:
                    p.unlink()
                except FileNotFoundError:
                    pass
    result = {'removed_blocks': len(removed), 'removed_bytes': removed_bytes,
              'closed_dead_readers': dead_readers,
              'removed_tmp_files': 0 if dry_run else len(tmp_files),
              'removed_tmp_bytes': 0 if dry_run else tmp_bytes,
              'kept_blocks': len(protected), 'dry_run': dry_run,
              'protected_generations': sorted(protected_gens),
              'removed': removed}
    if hook:
        hook('gc_done', result)
    return result


# ---------------------------------------------------------------------------
# Storage accounting per generation / representation
# ---------------------------------------------------------------------------

def generation_layout(db, generation_id, source_id=None, store_root=None):
    """Per-manifest layout: ordered blocks, unique block count and overhead."""
    if store_root is None:
        store_root = store_root_for(db)
    sql = ('SELECT m.*,s.path,s.version AS source_version FROM source_manifest m '
           'JOIN source s ON s.id=m.source_id WHERE m.generation_id=?')
    args = [generation_id]
    if source_id is not None:
        sql += ' AND m.source_id=?'
        args.append(source_id)
    sql += ' ORDER BY m.source_id'
    rows = db.execute(sql, args).fetchall()
    block_store = BlockStore(store_root)
    out = []
    for row in rows:
        blocks = _manifest_blocks(db, row['id'])
        sizes = []
        hash_list = []
        for b in blocks:
            hash_list.append(b['block_sha'])
            try:
                sizes.append(block_store.path(b['block_sha']).stat().st_size)
            except FileNotFoundError:
                sizes.append(0)
        out.append({
            'source_id': row['source_id'],
            'path': row['path'],
            'version': row['source_version'],
            'sha256': row['sha256'],
            'length': row['length'],
            'representation': row['representation'],
            'block_bytes': row['block_bytes'],
            'block_count': row['block_count'],
            'block_hashes': hash_list,
            'block_sizes': sizes,
            'prefix_bytes': len(row['prefix'] or b''),
            'suffix_bytes': len(row['suffix'] or b''),
            'manifest_bytes': len(row['payload'].encode('utf-8')),
        })
    return out


def generation_accounting(db, generation_id, store_root=None):
    if store_root is None:
        store_root = store_root_for(db)
    block_store = BlockStore(store_root)
    layout = generation_layout(db, generation_id, store_root=store_root)
    hashes = set()
    logical_refs = 0
    manifest_bytes = 0
    prefix_bytes = 0
    suffix_bytes = 0
    for item in layout:
        hashes.update(item['block_hashes'])
        logical_refs += item['block_count']
        manifest_bytes += item['manifest_bytes']
        prefix_bytes += item['prefix_bytes']
        suffix_bytes += item['suffix_bytes']
    unique_bytes = 0
    for digest in hashes:
        try:
            unique_bytes += block_store.path(digest).stat().st_size
        except FileNotFoundError:
            pass
    gen = _generation(db, generation_id)
    return {
        'generation': generation_id,
        'status': gen['status'],
        'representation': gen['representation'],
        'sources': len(layout),
        'logical_block_refs': logical_refs,
        'unique_blocks': len(hashes),
        'unique_block_bytes': unique_bytes,
        'source_bytes': sum(i['length'] for i in layout),
        'manifest_bytes': manifest_bytes,
        'prefix_bytes': prefix_bytes,
        'suffix_bytes': suffix_bytes,
        'layout': layout,
    }


def accounting(db, generation_id=None, store_root=None):
    """Archive-wide physical accounting plus per-generation layout."""
    if store_root is None:
        store_root = store_root_for(db)
    if generation_id is None:
        generation_id = active_generation(db)
    physical = physical_usage(db, store_root)
    gens = []
    for row in db.execute('SELECT * FROM archive_generation ORDER BY id'):
        gens.append({'id': row['id'], 'status': row['status'],
                     'representation': row['representation'],
                     'block_bytes': row['block_bytes'],
                     'created_at': row['created_at'],
                     'published_at': row['published_at'],
                     'retired_at': row['retired_at']})
    whole_file = db.execute(
        'SELECT count(*) AS sources, count(DISTINCT sha256) AS unique_sha, '
        'COALESCE(sum(length),0) AS bytes FROM source').fetchone()
    # Whole-file SHA dedupe can only save a file whose *entire* bytes repeat.
    # source.sha256 is UNIQUE, so on this fixture (and in general for distinct
    # files of different length) that saving is exactly zero.
    redundant = 0
    if whole_file['sources'] != whole_file['unique_sha']:
        distinct_bytes = db.execute(
            'SELECT COALESCE(sum(length),0) FROM '
            '(SELECT length FROM source GROUP BY sha256)').fetchone()[0]
        redundant = whole_file['bytes'] - distinct_bytes
    result = {
        'store_root': str(store_root),
        'active_generation': active_generation(db),
        'generations': gens,
        'physical': physical,
        'blobs': blob_usage(db),
        'manifest_rows': db.execute('SELECT count(*) FROM source_manifest').fetchone()[0],
        'manifest_block_refs': db.execute('SELECT count(*) FROM manifest_block').fetchone()[0],
        'whole_file_sha_dedup': {
            'sources': whole_file['sources'],
            'unique_file_sha': whole_file['unique_sha'],
            'savable_bytes': redundant,
        },
        'open_readers': db.execute(
            'SELECT count(*) FROM archive_reader WHERE closed_at IS NULL').fetchone()[0],
    }
    if generation_id is not None:
        result['generation'] = generation_accounting(db, generation_id, store_root)
    return result


# ---------------------------------------------------------------------------
# Migration orchestration
# ---------------------------------------------------------------------------

def migrate(db_path, representation='record_chunks', store_root=None, hook=None,
            release=True, collect=True, vacuum_after=True, retries=5):
    """Online migration with resumable, compare-and-swap publication.

    Crash points (via ``hook``) leave the database in one of these states, each
    of which the next call resumes idempotently:

    * some/all blocks written but manifests rolled back -> rebuild;
    * manifests committed but no pointer switch -> publish;
    * pointer switched but BLOBs present -> verify + release;
    * BLOBs cleared -> verification reads manifests, GC is safe.
    """
    db = connect(db_path)
    try:
        if store_root is not None:
            root = set_store_root(db, store_root)
        else:
            root = store_root_for(db, persist=True)
        before = physical_usage(db, root)
        gen = None
        expected = None
        build_info = None
        pub = None
        for attempt in range(max(1, retries)):
            gen, expected = plan_generation(db, representation)
            build_info = build(db, root, gen, hook=hook)
            try:
                pub = publish(db, gen, root, verify=True, expected_pointer=expected)
                break
            except ValueError as exc:
                if str(exc) != 'generation_pointer_conflict' or attempt + 1 >= retries:
                    raise
                # Another importer published while we built; replan against
                # the new pointer.  Blocks are content-addressed, so the
                # existing manifests can be reused when the representation
                # still matches.
                continue
        verification = verify_generation(db, root, gen)
        if not verification['ok']:
            raise ValueError('archive_verification_failed')
        if hook:
            hook('published', {'generation': gen, 'publish': pub})
        release_info = None
        if release:
            release_info = release_blobs(db, gen, root, verify=True,
                                         vacuum_after=vacuum_after, hook=hook)
        gc_info = None
        if collect:
            gc_info = gc(db, root, hook=hook)
        after = physical_usage(db, root)
        return {
            'generation': gen,
            'representation': representation,
            'build': build_info,
            'publish': pub,
            'verification': verification,
            'release': release_info,
            'gc': gc_info,
            'physical_before': before,
            'physical_after': after,
            'block_store': BlockStore(root).usage(),
            'accounting': accounting(db, gen, root),
        }
    finally:
        db.close()


def archive_sources(db, store_root=None, representation=None, hook=None, release=True):
    """Archive sources that have no manifest in the active generation.

    Used by ingest so a new import immediately gets blocks and a manifest.  A
    new generation is created (carrying forward the previous manifests by
    reference) and published with the same compare-and-swap pointer, so an
    import never mutates an already published snapshot.  After the whole
    generation verifies, the newly imported sources' BLOBs are cleared (their
    bytes now live in verified blocks); older BLOBs are left to the explicit
    ``release_blobs`` rollout step.
    """
    if store_root is not None:
        root = set_store_root(db, store_root)
    else:
        root = store_root_for(db, persist=True)
    with _write(db):
        active = active_generation(db)
        active_gen = _generation(db, active) if active is not None else None
        rep = representation or (active_gen['representation'] if active_gen else 'record_chunks')
        rep = _normalise_representation(rep)
        if active is not None:
            pending = db.execute(
                'SELECT s.id FROM source s WHERE NOT EXISTS '
                '(SELECT 1 FROM generation_manifest gm WHERE gm.generation_id=? AND gm.source_id=s.id) '
                'ORDER BY s.id', (active,)).fetchall()
            if not pending and active_gen['representation'] == rep:
                return {'generation': active, 'archived': 0, 'switched': False}
        else:
            pending = db.execute('SELECT id FROM source ORDER BY id').fetchall()
        block_bytes = CHUNK
        gen_id = db.execute(
            "INSERT INTO archive_generation(status,representation,block_bytes,plan,created_at) "
            "VALUES ('planned',?,?,?,?)",
            (rep, block_bytes,
             json.dumps({'sources': [r['id'] for r in pending]}), _now())).lastrowid
        gen = dict(db.execute('SELECT * FROM archive_generation WHERE id=?', (gen_id,)).fetchone())
        if active is not None and active_gen['representation'] == rep:
            db.execute(
                'INSERT INTO generation_manifest(generation_id,source_id,manifest_id) '
                'SELECT ?,source_id,manifest_id FROM generation_manifest WHERE generation_id=?',
                (gen_id, active))
        archived = 0
        new_source_ids = []
        for src in db.execute('SELECT * FROM source ORDER BY id').fetchall():
            linked = db.execute(
                'SELECT 1 FROM generation_manifest WHERE generation_id=? AND source_id=?',
                (gen_id, src['id'])).fetchone()
            if linked:
                continue
            _insert_manifest_locked(db, root, gen, dict(src), hook=hook)
            archived += 1
            new_source_ids.append(src['id'])
        if _missing_manifests(db, gen_id):
            raise ValueError('generation_incomplete')
        # Verify the new manifests before exposing the new generation.
        check = verify_generation(db, root, gen_id)
        if not check['ok']:
            raise ValueError('archive_verification_failed')
        cleared = []
        if release:
            for source_id in new_source_ids:
                row = db.execute('SELECT raw FROM source WHERE id=?', (source_id,)).fetchone()
                if row is not None and row['raw'] is not None:
                    db.execute("UPDATE source SET raw=NULL,storage='blocks' WHERE id=?",
                               (source_id,))
                    cleared.append(source_id)
        if active is not None:
            db.execute("UPDATE archive_generation SET status='retired',retired_at=? WHERE id=?",
                       (_now(), active))
        db.execute("UPDATE archive_generation SET status='published',published_at=? WHERE id=?",
                   (_now(), gen_id))
        db.execute('INSERT INTO archive_state(key,value) VALUES (?,?) '
                   'ON CONFLICT(key) DO UPDATE SET value=excluded.value',
                   (POINTER_KEY, str(gen_id)))
    return {'generation': int(gen_id), 'archived': archived, 'switched': True,
            'previous': active, 'blobs_cleared': cleared}


# ---------------------------------------------------------------------------
# Frozen case registration (pre-migration identity proof)
# ---------------------------------------------------------------------------

def register(db_path, case_id, fixture):
    """Freeze source ids, paths, lengths and SHAs without touching storage."""
    source = Path(fixture).resolve()
    raw = source.read_bytes()
    doc = json.loads(raw)
    if not case_id or not doc.get('sources') or not doc.get('representations'):
        raise ValueError('bad_archive_case')
    db = connect(db_path)
    try:
        with _write(db):
            records = []
            for name in doc['sources']:
                row = db.execute('SELECT * FROM source WHERE path=?',
                                 (str((source.parent / name).resolve()),)).fetchone()
                if row is None:
                    raise ValueError('archive_source_missing')
                original = bytes(row['raw']) if row['raw'] is not None else None
                if original is not None and sha256(original) != row['sha256']:
                    raise ValueError('archive_source_hash_mismatch')
                records.append({
                    'id': row['id'], 'path': row['path'], 'sha256': row['sha256'],
                    'length': len(original) if original is not None else row['length'],
                    'version': row['version'], 'storage': row['storage'],
                })
            payload = json.dumps({'fixture': str(source), 'digest': sha256(raw),
                                  'doc': doc, 'sources': records},
                                 sort_keys=True, separators=(',', ':'))
            old = db.execute('SELECT payload FROM archive_case WHERE id=?', (case_id,)).fetchone()
            if old and old['payload'] != payload:
                raise ValueError('archive_case_conflict')
            db.execute('INSERT OR IGNORE INTO archive_case VALUES (?,?)', (case_id, payload))
    finally:
        db.close()
    return {'case_id': case_id, 'sources': len(records)}


def frozen_case(db, case_id):
    row = db.execute('SELECT payload FROM archive_case WHERE id=?', (case_id,)).fetchone()
    if row is None:
        raise ValueError('archive_case_missing')
    return json.loads(row['payload'])


def check_case(db, store_root, case_id, reader=None):
    """Reconstruct each frozen source and compare to its recorded identity."""
    payload = frozen_case(db, case_id)
    own_reader = False
    if reader is None:
        reader = open_reader(db, store_root)
        own_reader = True
    checks = []
    try:
        for record in payload['sources']:
            try:
                data = read_source(db, record['id'], store_root=store_root, reader=reader)
            except Exception as exc:
                checks.append({'id': record['id'], 'ok': False, 'error': str(exc)})
                continue
            digest = sha256(data)
            checks.append({
                'id': record['id'], 'path': record['path'], 'length': len(data),
                'sha256': digest,
                'ok': (len(data) == record['length'] and digest == record['sha256']),
            })
    finally:
        if own_reader and reader is not None:
            reader.close()
    return {'case_id': case_id, 'ok': all(c['ok'] for c in checks)
            and len(checks) == len(payload['sources']), 'sources': checks}
