"""Reproducible local setup and read-only readiness checks, without model calls."""

import hashlib
import secrets
from uuid import uuid4
import importlib.util
import json
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import inspect, text

from inkwise.contracts import Rubric
from inkwise.database import Base, Identity, TenantQuota, create_session_factory, initialize_database
from inkwise.errors import DomainError
from inkwise.rubrics import canonical, digest, safe_segment
from inkwise.settings import Settings


def write_private(path: Path, content: str) -> None:
    """Write once, refuse symlinks/overwrites, keep permissions at 0600."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        file.write(content)


def provision(settings: Settings, tenant: str, classrooms: list[str], role: str, output: Path) -> str:
    """Provision an operator-approved identity; never return its secret token."""
    safe_segment(tenant)
    for classroom in classrooms:
        safe_segment(classroom)
    token, user_id = secrets.token_urlsafe(32), str(uuid4())
    engine, sessions = create_session_factory(settings.database_url)
    try:
        with sessions.begin() as db:
            if db.get(TenantQuota, tenant) is None:
                db.add(TenantQuota(tenant_id=tenant))
            db.add(Identity(id=user_id, tenant_id=tenant, role=role, classroom_ids=classrooms,
                            token_hash=hashlib.sha256(token.encode()).hexdigest()))
            db.flush()
            # Failed writes roll back the identity. Failed commits leave an inert token.
            write_private(output, token + "\n")
    finally:
        engine.dispose()
    return user_id


def load_teaching_pack(folder: Path) -> tuple[dict, list[Rubric]]:
    """Validate bounded local candidates; never interpret them as approved rules."""
    folder = folder.resolve()

    def read(relative: str, maximum: int = 262144) -> str:
        path = (folder / relative).resolve()
        if not path.is_relative_to(folder) or path.stat().st_size > maximum:
            raise DomainError("TEACHING_PACK_INVALID")
        return path.read_text()

    manifest = json.loads(read("manifest.json"))
    if manifest.get("reviewStatus") != "candidate" or not 1 <= len(manifest["rubrics"]) <= 100:
        raise DomainError("TEACHING_PACK_INVALID")
    source_ids = {source["id"] for source in json.loads(read("sources.json"))["sources"]}
    rubrics = []
    for entry in manifest["rubrics"]:
        raw = read(entry["file"])
        rubric = Rubric.model_validate_json(raw)
        if (digest(raw) != entry["sha256"] or rubric.id != entry["id"] or not rubric.synthetic
                or entry.get("schoolApproved") is not False or entry.get("status") != "draft"
                or not set(entry["sourceIds"]) <= source_ids):
            raise DomainError("TEACHING_PACK_INVALID")
        rubrics.append(rubric)
    if len({rubric.id for rubric in rubrics}) != len(rubrics):
        raise DomainError("TEACHING_PACK_INVALID")
    raw = read(manifest["lexiconFile"], 10_000_000)
    words = json.loads(raw)
    if (digest(raw) != manifest["lexiconSha256"] or words.get("reviewStatus") != "candidate"
            or len(words["entries"]) != manifest["lexiconEntries"]):
        raise DomainError("TEACHING_PACK_INVALID")
    for extra in manifest.get('supplementalLexicons',[]):
        raw=read(extra['file'],1_000_000);words=json.loads(raw)
        if digest(raw)!=extra['sha256'] or words.get('reviewStatus')!='candidate' or len(words['entries'])!=extra['entries']:
            raise DomainError('TEACHING_PACK_INVALID')
        if any(row.get('language')!=extra['language'] or not row.get('incorrect') or not row.get('correct') for row in words['entries']):
            raise DomainError('TEACHING_PACK_INVALID')
    examples = json.loads(read(manifest["examplesFile"]))
    if examples.get("teacherValidated") is not False:
        raise DomainError("TEACHING_PACK_INVALID")
    by_id = {rubric.id: rubric for rubric in rubrics}
    for example in examples["examples"]:
        if example["rubricId"] not in by_id or example.get("scores") is not None:
            raise DomainError("TEACHING_PACK_INVALID")
    return manifest, rubrics


def teaching_readiness(folder: Path) -> dict:
    """Content-free candidate coverage; never certify a school or model."""
    manifest, rubrics = load_teaching_pack(folder)
    example_path = folder / manifest["examplesFile"]
    if example_path.stat().st_size > 262144:
        raise DomainError("TEACHING_PACK_INVALID")
    raw = example_path.read_bytes()
    examples = json.loads(raw)["examples"]
    counts = {rubric.id: 0 for rubric in rubrics}
    for example in examples:
        counts[example["rubricId"]] += 1
    if not all(counts.values()):
        raise DomainError("TEACHING_COVERAGE_INCOMPLETE")
    return {"scope": "synthetic-candidate-readiness", "syntheticChecksPassed": True,
            "schoolAccepted": False, "teacherApprovalVerified": False,
            "realProviderValidated": False, "rubricCount": len(rubrics),
            "manifestSha256": digest((folder / "manifest.json").read_text()),
            "examplesSha256": hashlib.sha256(raw).hexdigest(),
            "coverage": [{"rubricId": rubric.id, "gradeMin": rubric.grade_min,
                          "gradeMax": rubric.grade_max, "languagePolicy": rubric.language_policy,
                          "genre": rubric.genre, "syntheticExamples": counts[rubric.id]}
                         for rubric in rubrics],
            "remainingGateCodes": ["SCHOOL_RULES_APPROVAL_REQUIRED", "AUTHORIZED_MATERIALS_REQUIRED",
                                   "AUTHORIZED_BLIND_TEACHER_REVIEW_REQUIRED", "HANDWRITING_STRATA_REQUIRED",
                                   "REAL_PROVIDER_EVALUATION_REQUIRED"]}


def setup_local(destination: Path, teaching: Path, *, frontend_dir: Path | None = None,
                vision_executable: Path | None = None) -> dict:
    """Create a fresh isolated workspace; do not alter existing data or start services."""

    manifest, rubrics = load_teaching_pack(teaching)
    if frontend_dir is not None and not (frontend_dir / "index.html").is_file():
        raise DomainError("LOCAL_RUNTIME_ASSET_MISSING")
    if vision_executable is not None and not (vision_executable.is_file() and os.access(vision_executable, os.X_OK)):
        raise DomainError("LOCAL_RUNTIME_ASSET_MISSING")
    destination = destination.absolute()
    if destination.exists() or destination.is_symlink():
        raise DomainError("LOCAL_WORKSPACE_ALREADY_EXISTS")
    if any(character in str(destination) for character in "\n\r\x00$"):
        raise DomainError("LOCAL_WORKSPACE_PATH_INVALID")
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    staging = Path(tempfile.mkdtemp(prefix=".inkwise-setup-", dir=destination.parent))
    try:
        settings = Settings(database_url=f"sqlite:///{staging}/inkwise.sqlite3",
                            data_dir=staging, rubric_dir=staging / "rules")
        initialize_database(settings.database_url)
        identities = {}
        for role in ("teacher", "reviewer", "admin"):
            identities[role] = provision(settings, "local-demo", ["practice"], role, staging / f"{role}-access.txt")
        rules = staging / "rules" / "local-demo"
        rules.mkdir(parents=True)
        active = {}
        for rubric in rubrics:
            filename = rubric.id + ".json"
            raw = canonical(rubric.model_dump(mode="json", by_alias=True))
            (rules / filename).write_text(raw)
            active[rubric.id] = {"file": filename, "sha256": digest(raw)}
        (rules / "active.json").write_text(canonical(active))
        configuration = {
            "DATABASE_URL": f"sqlite:///{destination}/inkwise.sqlite3",
            "DATA_DIR": str(destination), "RUBRIC_DIR": str(destination / "rules"),
            "MODEL_BASE_URL": "http://127.0.0.1:11434/v1", "MODEL_NAME": "inkwise-deepseek-local",
            "MODEL_STRUCTURED_OUTPUT": "json_schema", "MODEL_TIMEOUT_SECONDS": 120,
            "MODEL_MAX_OUTPUT_TOKENS": 4096, "MODEL_CONCURRENCY": 1, "MODEL_GLOBAL_CONCURRENCY": 1,
            "MODEL_CONTEXT_TOKENS": 65536, "CLOUD_ENABLED": False, "MAX_CLOUD_COST_MICROUSD": 0,
            "CALIBRATION_DIR": str(destination / "calibration"),
            "REFERENCE_PACK": str((teaching / "reference-pack.json").resolve()),
            "MODEL_CALLS_PER_JOB": 12, "JOB_TIMEOUT_SECONDS": 600,
            "EMBEDDING_BASE_URL": "http://127.0.0.1:11434/v1", "EMBEDDING_MODEL": "bge-m3",
            "OCR_BACKEND": "disabled", "NEO4J_URI": "", "NEO4J_USER": "", "NEO4J_PASSWORD": "",
            "CHROMA_HOST": "", "LEXICON_MODEL_REVISION": "", "MODEL_API_KEY": "",
            "EVALUATION_SALT": secrets.token_urlsafe(32),
            "EVOLUTION_ENABLED": False, "EVOLUTION_TENANT": "local-demo",
            "KAFKA_ENABLED": False, "KAFKA_BOOTSTRAP_SERVERS": "",
        }
        if frontend_dir is not None:
            configuration["SERVED_FRONTEND_DIR"] = str(frontend_dir.resolve())
        if vision_executable is not None:
            configuration.update(OCR_BACKEND="vision", VISION_EXECUTABLE=str(vision_executable.resolve()))
        lines = [f"INKWISE_{key}={json.dumps(str(value).lower() if isinstance(value, bool) else str(value), ensure_ascii=False)}"
                 for key, value in configuration.items()]
        write_private(staging / ".env", "\n".join(lines) + "\n")
        summary = {"tenant": "local-demo", "classroom": "practice", "identities": identities,
                   "rubricRelease": manifest["release"], "rubrics": len(rubrics),
                   "schoolApproved": False, "externalLexiconActivated": False}
        write_private(staging / "setup.json", canonical(summary))
        # Destination was checked above; rename never overwrites a nonempty directory.
        if destination.exists() or destination.is_symlink():
            raise DomainError("LOCAL_WORKSPACE_ALREADY_EXISTS")
        staging.rename(destination)
        return summary
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def doctor(settings: Settings, *, connect: bool = False) -> dict:
    """Return redacted statuses. Optional GETs never send essays or generate tokens."""
    checks = []

    def add(name: str, status: str, detail: str) -> None:
        checks.append({"name": name, "status": status, "detail": detail})

    from inkwise.settings import model_configured
    configured = model_configured(settings)
    add("model", "pass" if configured else "fail", "configured" if configured else "set model endpoint and name")
    add("frontend", "pass" if (settings.served_frontend_dir / "index.html").is_file() else "fail", "workspace files")
    sqlite_path = (Path(settings.database_url.removeprefix("sqlite:///"))
                   if settings.database_url.startswith("sqlite:///") else None)
    if sqlite_path and not sqlite_path.is_file():
        add("database", "fail", "initialize the selected database explicitly")
    elif sqlite_path or connect:
        engine = None
        try:
            engine, _ = create_session_factory(settings.database_url)
            with engine.connect() as connection:
                connection.execute(text("SELECT 1"))
                required = set(Base.metadata.tables)
                complete = required <= set(inspect(connection).get_table_names())
            add("database", "pass" if complete else "fail", "schema present" if complete else
                "schema incomplete; stop the workspace, back up and run upgrade-check")
        except Exception:
            add("database", "fail", "connection or schema unavailable; private details suppressed")
        finally:
            if engine is not None:
                engine.dispose()
    else:
        add("database", "warning", "remote database not probed without --connect")
    from inkwise.rubrics import RubricRepository
    try:
        manifests = list(settings.rubric_dir.glob("*/active.json"))
        repository = RubricRepository(settings.rubric_dir)
        rules_ready = bool(manifests) and all(repository.list(path.parent.name) for path in manifests)
    except Exception:
        rules_ready = False
    add("rules", "pass" if rules_ready else "fail", "verified rule manifests; school approval is separate")
    from inkwise.ocr import ocr_configured
    ocr_ready = ocr_configured(settings)
    add("ocr", "pass" if ocr_ready else "warning", "configured" if ocr_ready else "text entry works; image OCR disabled/unconfigured")
    retrieval_ready = bool(settings.neo4j_uri and settings.chroma_host and settings.embedding_base_url)
    add("retrieval", "pass" if retrieval_ready else "warning",
        "configured, service/index validation separate" if retrieval_ready else "local rule snapshot fallback; no external examples")
    add("lexicon", "pass" if settings.lexicon_model_revision else "warning", "only reviewed tenant indexes are activated")
    if settings.evolution_enabled:
        ready=bool(settings.evolution_tenant and len(settings.evaluation_salt.get_secret_value())>=32)
        add('evolution','pass' if ready else 'fail','explicit tenant and private evaluation salt required')
    if settings.kafka_enabled:
        add("kafka", "pass" if settings.kafka_bootstrap_servers and importlib.util.find_spec("aiokafka") else "fail",
            "chunk-worker and broker needed; delivery not tested here")
        add("long-text-budget", "pass" if settings.model_calls_per_job >= 22 else "warning",
            "15000 characters may need 22+ calls; actual chunk count determines acceptance")
    if connect and configured:
        import httpx
        from inkwise.provider_io import service_url

        try:
            base,name,key=(settings.model_base_url,settings.model_name,settings.model_api_key) if settings.model_base_url and settings.model_name else (settings.cloud_model_base_url,settings.cloud_model_name,settings.cloud_model_api_key)
            headers = {}
            if key.get_secret_value():
                headers["Authorization"] = "Bearer " + key.get_secret_value()
            with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
                response = client.get(service_url(base, "models"), headers=headers)
                response.raise_for_status()
                identifiers = {row["id"] for row in response.json()["data"]}
            available = (name in identifiers or (":" not in name and name + ":latest" in identifiers))
            add("model-catalog", "pass" if available else "fail", "model ID lookup; no inference")
        except Exception:
            add("model-catalog", "fail", "catalog unavailable; no model call made or private response printed")
    return {"ready": not any(check["status"] == "fail" for check in checks),
            "scope": "configuration readiness, not grading or school acceptance", "checks": checks}


def ensure_loopback_port_available(port: int) -> None:
    """Match the server's address reuse while still refusing an active listener."""
    import socket
    with socket.socket() as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(('127.0.0.1', port))
        except OSError:
            raise DomainError('LOCAL_PORT_UNAVAILABLE') from None


def run_local(env_file: Path, port: int) -> int:
    """Run API and worker together on loopback, terminating both on exit."""
    import signal
    import subprocess
    import sys
    import time

    settings = Settings(_env_file=env_file)
    if not env_file.is_file() or not doctor(settings)["ready"]:
        raise DomainError("LOCAL_CONFIGURATION_NOT_READY")
    ensure_loopback_port_available(port)
    command = [sys.executable, "-m", "inkwise", "--env-file", str(env_file.resolve())]
    children = []

    def stop(_signum, _frame):
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGTERM, stop)
    try:
        children.append(subprocess.Popen(command + ["serve", "--port", str(port)]))
        children.append(subprocess.Popen(command + ["worker"]))
        if settings.evolution_enabled:
            children.append(subprocess.Popen(command+["evolution-worker","--tenant",settings.evolution_tenant]))
        print(f"Local workspace: http://127.0.0.1:{port}/workspace/ (Ctrl+C stops API and worker)", flush=True)
        while all(child.poll() is None for child in children):
            time.sleep(0.25)
        return next((child.returncode or 1 for child in children if child.poll() is not None), 1)
    except KeyboardInterrupt:
        return 0
    finally:
        signal.signal(signal.SIGTERM, previous)
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()
