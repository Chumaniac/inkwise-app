"""Build a resumable local-only BGE-M3 candidate index; never activate school data."""

import argparse
import asyncio
import json
import sqlite3
import struct
from pathlib import Path

import httpx

from inkwise.lexicon import build_index, nearest
from inkwise.retrieval import Embeddings
from inkwise.rubrics import canonical, digest
from inkwise.settings import Settings
from inkwise.setup import load_teaching_pack


async def build(teaching: Path, output: Path) -> dict:
    """Persist each successful batch so an interruption does not repeat embeddings."""
    manifest, _ = load_teaching_pack(teaching)
    document = json.loads((teaching / manifest["lexiconFile"]).read_text())
    entries = document["entries"]
    # This command has no cloud endpoint option and sends only public word pairs.
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        response = await client.get("http://127.0.0.1:11434/api/tags")
        response.raise_for_status()
        models = response.json()["models"]
    revision = next(model["digest"] for model in models if model["name"] in ("bge-m3", "bge-m3:latest"))
    output.mkdir(parents=True, exist_ok=True)
    index = output / f"candidates-{manifest['lexiconSha256'][:12]}-{revision.split(':')[-1][:12]}.sqlite"
    cache = sqlite3.connect(output / "embedding-cache.sqlite")
    encoder = Embeddings(Settings(embedding_base_url="http://127.0.0.1:11434/v1", embedding_model="bge-m3"))
    generated = 0
    try:
        cache.execute("CREATE TABLE IF NOT EXISTS vectors (revision TEXT, key TEXT, vector BLOB, PRIMARY KEY(revision,key))")
        texts = [canonical({key: row.get(key, "") for key in ("incorrect", "correct", "language", "explanation")}) for row in entries]
        keys = [digest(value) for value in texts]
        if not index.exists():
            for start in range(0, len(texts), 16):
                batch = list(range(start, min(start + 16, len(texts))))
                missing = [i for i in batch if not cache.execute("SELECT 1 FROM vectors WHERE revision=? AND key=?", (revision, keys[i])).fetchone()]
                if missing:
                    vectors = await encoder.embed([texts[i] for i in missing])
                    with cache:
                        cache.executemany("INSERT INTO vectors VALUES (?, ?, ?)",
                            [(revision, keys[i], struct.pack("<1024f", *vector)) for i, vector in zip(missing, vectors)])
                    generated += len(missing)
                if start % 512 == 0:
                    print(f"Candidate embeddings persisted: {min(start + 16, len(texts))}/{len(texts)}", flush=True)
            vectors = [cache.execute("SELECT vector FROM vectors WHERE revision=? AND key=?", (revision, key)).fetchone()[0] for key in keys]
            from uuid import uuid4
            temporary = output / f"building-{uuid4().hex}.sqlite"
            try:
                build_index(temporary, entries, vectors, manifest["lexiconSha256"], revision, "candidate-only", keys)
                import os
                os.link(temporary, index)
            finally:
                temporary.unlink(missing_ok=True)
        # Reuse a cached vector to verify exact indexed content, not retrieval quality.
        vector = struct.unpack("<1024f", cache.execute("SELECT vector FROM vectors WHERE revision=? AND key=?", (revision, keys[0])).fetchone()[0])
        hits = nearest(index, list(vector), model_revision=revision, tenant="candidate-only", source_hash=manifest["lexiconSha256"], limit=1)
        if hits[0]["entry"] != entries[0]:
            raise ValueError("Candidate index round-trip failed")
        result = {"entries": len(entries), "newEmbeddings": generated, "reusedEmbeddings": len(entries) - generated,
                  "modelRevision": revision, "sourceSha256": manifest["lexiconSha256"], "indexFile": index.name,
                  "indexBytes": index.stat().st_size, "status": "candidate-only", "schoolActivated": False,
                  "verification": "exact-vector round-trip; not contextual retrieval precision or teaching validation"}
        (output / "receipt.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        return result
    finally:
        cache.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--teaching", type=Path, default=Path("teaching"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/teaching-index"))
    args = parser.parse_args()
    print(json.dumps(asyncio.run(build(args.teaching, args.output)), indent=2))
