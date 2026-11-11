"""Tenant-local sqlite-vec indexes rebuilt from a single versioned source."""

import json
import math
import sqlite3
import struct
from pathlib import Path

from inkwise.errors import DomainError
from inkwise.rubrics import digest


def connect_index(path: Path):
    import sqlite_vec
    db = sqlite3.connect(path)
    if not hasattr(db, "enable_load_extension"):
        db.close()
        import apsw
        db = apsw.Connection(str(path))
    db.enable_load_extension(True)
    try:
        sqlite_vec.load(db)
    finally:
        db.enable_load_extension(False)
    return db


def build_index(path: Path, entries: list[dict], vectors: list, source_hash: str, model_revision: str,
                tenant: str = "", embedding_keys: list[str] | None = None):
    """Create a new index only; existing active indexes cannot be overwritten."""
    if path.exists() or len(entries) != len(vectors) or len(entries) > 100000:
        raise DomainError("LEXICON_BUILD_INVALID")
    for vector in vectors:
        if isinstance(vector,bytes) and len(vector)!=4096:raise DomainError("LEXICON_VECTOR_INVALID")
        values=struct.unpack('<1024f',vector) if isinstance(vector,bytes) and len(vector)==4096 else vector
        if len(values)!=1024 or any(not math.isfinite(n) for n in values):raise DomainError("LEXICON_VECTOR_INVALID")
    keys=embedding_keys or [digest(json.dumps(entry,sort_keys=True,ensure_ascii=False)) for entry in entries]
    if len(keys)!=len(entries) or len(set(keys))!=len(keys):raise DomainError("LEXICON_DUPLICATE_ENTRY")
    import sqlite_vec
    path.parent.mkdir(parents=True, exist_ok=True)
    db = connect_index(path)
    try:
        with db:
            db.execute("CREATE TABLE metadata(source_hash TEXT, model_revision TEXT, tenant_id TEXT)")
            db.execute("INSERT INTO metadata VALUES (?, ?, ?)", (source_hash, model_revision,tenant))
            db.execute("CREATE TABLE entries(id INTEGER PRIMARY KEY, payload TEXT NOT NULL, embedding_key TEXT UNIQUE)")
            db.execute("CREATE VIRTUAL TABLE embeddings USING vec0(embedding float[1024])")
            for index, (entry, vector) in enumerate(zip(entries, vectors), 1):
                db.execute("INSERT INTO entries VALUES (?, ?, ?)", (index, json.dumps(entry, ensure_ascii=False),keys[index-1]))
                db.execute("INSERT INTO embeddings(rowid, embedding) VALUES (?, ?)",
                            (index, vector if isinstance(vector,bytes) else sqlite_vec.serialize_float32(vector)))
    finally:
        db.close()


def nearest(path: Path, vector: list[float], *, model_revision: str, limit: int = 5, tenant: str | None = None,
            source_hash: str | None = None) -> list[dict]:
    if not path.is_file() or len(vector) != 1024 or not 1 <= limit <= 20:
        raise DomainError("LEXICON_QUERY_INVALID")
    if any(not math.isfinite(n) for n in vector):
        raise DomainError("LEXICON_VECTOR_INVALID")
    import sqlite_vec
    db = connect_index(path)
    try:
        stored = db.execute("SELECT model_revision FROM metadata").fetchone()
        if not stored or stored[0] != model_revision:
            raise DomainError("EMBEDDING_VERSION_MISMATCH")
        if tenant is not None and db.execute("SELECT tenant_id FROM metadata").fetchone()[0]!=tenant:
            raise DomainError("LEXICON_TENANT_MISMATCH")
        if source_hash is not None and db.execute("SELECT source_hash FROM metadata").fetchone()[0]!=source_hash:
            raise DomainError("LEXICON_SOURCE_MISMATCH")
        matches = db.execute("SELECT rowid, distance FROM embeddings WHERE embedding MATCH ? AND k = ? ORDER BY distance",
                             (sqlite_vec.serialize_float32(vector), limit)).fetchall()
        return [{"entry": json.loads(db.execute("SELECT payload FROM entries WHERE id=?", (identifier,)).fetchone()[0]),
                 "distance": distance} for identifier, distance in matches]
    finally:
        db.close()


async def build_reviewed_lexicon(settings,tenant: str,source: Path,revision: str,submitted_by: str,approved_by: str,embedder=None):
    """Rebuild an immutable release, embedding only changed entries.

    Pilot limit: 10000 reviewed pairs / 10 MB source. Larger corpora require a
    streaming build; vectors are retained as float32 blobs, not Python float lists.
    """
    from sqlalchemy import select
    from inkwise.database import Identity,create_session_factory
    from inkwise.cache_storage import atomic_json
    from inkwise.retrieval import Embeddings
    from inkwise.rubrics import canonical,safe_segment
    safe_segment(tenant)
    if source.stat().st_size>10_000_000 or not revision or submitted_by==approved_by:
        raise DomainError("LEXICON_APPROVAL_REQUIRED")
    engine,sessions=create_session_factory(settings.database_url)
    try:
        with sessions() as db:
            author=db.get(Identity,submitted_by);reviewer=db.get(Identity,approved_by)
            if (not author or not reviewer or not author.active or not reviewer.active
                or author.tenant_id!=tenant or reviewer.tenant_id!=tenant or reviewer.role not in ('reviewer','admin')):
                raise DomainError('LEXICON_APPROVAL_REQUIRED')
    finally:engine.dispose()
    document=json.loads(source.read_text());entries=document.get('entries',[])
    if document.get('reviewStatus', 'approved') != 'approved':
        raise DomainError('LEXICON_CANDIDATE_NOT_APPROVED')
    if not document.get('provenance') or not 1<=len(entries)<=10000:raise DomainError('LEXICON_SOURCE_INVALID')
    for entry in entries:
        if (not isinstance(entry,dict) or entry.get('language') not in ('zh','en','mixed')
            or not isinstance(entry.get('incorrect'),str) or not 1<=len(entry['incorrect'])<=120
            or not isinstance(entry.get('correct'),str) or not 1<=len(entry['correct'])<=120
            or not isinstance(entry.get('explanation',''),str) or len(entry.get('explanation',''))>500):
            raise DomainError('LEXICON_ENTRY_INVALID')
    source_hash=digest(canonical(document))
    folder=settings.data_dir/'lexicons'/tenant;folder.mkdir(parents=True,exist_ok=True,mode=0o700)
    filename=f'lexicon-{source_hash[:16]}-{digest(revision)[:8]}.sqlite'
    output=folder/filename
    active=folder/'active.json';cached={}
    baseline=active.read_text() if active.exists() else None
    if active.exists():
        old=json.loads(active.read_text())
        if old.get('modelRevision')==revision:
            database=connect_index(folder/safe_segment(old['file']))
            try:
                stored=database.execute('SELECT source_hash,model_revision,tenant_id FROM metadata').fetchone()
                if stored!=(old['sourceHash'],revision,tenant):raise DomainError('LEXICON_SOURCE_MISMATCH')
                cached={key:blob for key,blob in database.execute('SELECT e.embedding_key,v.embedding FROM entries e JOIN embeddings v ON e.id=v.rowid')}
            finally:database.close()
    texts=[canonical({k:e.get(k,'') for k in ('incorrect','correct','language','explanation')}) for e in entries]
    keys=[digest(t) for t in texts]
    if len(keys)!=len(set(keys)):raise DomainError('LEXICON_DUPLICATE_ENTRY')
    vectors=[cached.get(key) for key in keys]
    missing=[i for i,v in enumerate(vectors) if v is None]
    generated_count=0
    if not output.exists():
        encoder=embedder or Embeddings(settings)
        for start in range(0,len(missing),16):
            batch=missing[start:start+16]
            generated=await encoder.embed([texts[i] for i in batch])
            if len(generated)!=len(batch):raise DomainError('EMBEDDING_INVALID_RESPONSE')
            for i,vector in zip(batch,generated):vectors[i]=struct.pack('<1024f',*vector)
        from uuid import uuid4
        temporary=folder/('building-'+uuid4().hex+'.sqlite')
        try:
            build_index(temporary,entries,vectors,source_hash,revision,tenant,keys)
            import os
            try:os.link(temporary,output)
            except FileExistsError:pass
        finally:temporary.unlink(missing_ok=True)
        generated_count=len(missing)
    database=connect_index(output)
    try:
        if database.execute('SELECT source_hash,model_revision,tenant_id FROM metadata').fetchone()!=(source_hash,revision,tenant):
            raise DomainError('LEXICON_SOURCE_MISMATCH')
    finally:database.close()
    import fcntl
    with (folder/'.publish.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        current=active.read_text() if active.exists() else None
        if current!=baseline:raise DomainError('LEXICON_RELEASE_CHANGED')
        atomic_json(active,{'file':filename,'sourceHash':source_hash,'modelRevision':revision,'entries':len(entries),
                           'submittedBy':submitted_by,'approvedBy':approved_by,'provenance':document['provenance']})
    return {'entries':len(entries),'reusedEmbeddings':len(entries)-generated_count,'newEmbeddings':generated_count,'sourceHash':source_hash}
