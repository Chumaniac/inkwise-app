"""Operator commands; generated access tokens are written only to private files."""

import argparse
import json
import os
from pathlib import Path

from sqlalchemy import select

from inkwise.contracts import Rubric
from inkwise.core import DIMENSIONS
from inkwise.database import Identity, create_session_factory, initialize_database
from inkwise.rubrics import canonical, digest, safe_segment
from inkwise.settings import Settings
from inkwise.setup import provision, write_private


def seed_demo(settings: Settings, tenant: str) -> None:
    """Install synthetic example rubrics only when no active release exists."""
    folder = settings.rubric_dir / safe_segment(tenant)
    folder.mkdir(parents=True, exist_ok=True)
    if (folder / "active.json").exists():
        raise ValueError("An active rule release already exists; refusing overwrite")
    descriptions = {
        "content": "内容与任务：根据题目检查任务完成、事件或观点的具体性。",
        "coherence": "结构与连贯：整篇检查段落逻辑、衔接和首尾照应。",
        "expression": "表达与细节：根据体裁检查有效细节或论证，不强求模板。",
        "vocabulary": "词汇运用：结合语境判断准确性，豁免允许的语言切换和专名。",
        "grammar": "语法与规范：仅依据确认文本判断语法、时态、拼写及标点。",
    }
    manifest = {}
    for policy, genre, identifier in [("zh", "narrative", "chinese-demo"),
                                       ("en", "short-text", "english-demo"),
                                       ("mixed", "short-text", "mixed-demo")]:
        rubric = Rubric(id=identifier, release="synthetic-v1", title=f"{policy} 合成教学量规（非学校正式标准）",
                        language_policy=policy, grade_min=5, grade_max=12, genre=genre,
                        weights=dict(zip(DIMENSIONS, [30, 25, 20, 15, 10] if policy == "zh" else [25, 20, 15, 20, 20])),
                        rules=[{"id": f"{identifier}-R{i+1}", "dimension": dim, "description": descriptions[dim],
                                "levels": ["未达到该维度要求，提供原文依据。", "部分达到，存在明显不足。",
                                           "基本达到，但仍有多处可改进。", "较好达到，存在少量可改进。",
                                           "充分达到要求，提供具体证据。"]} for i, dim in enumerate(DIMENSIONS)])
        raw = canonical(rubric.model_dump(mode="json", by_alias=True))
        filename = identifier + "-synthetic-v1.json"
        (folder / filename).write_text(raw)
        manifest[identifier] = {"file": filename, "sha256": digest(raw)}
    (folder / "active.json").write_text(canonical(manifest))


def _main() -> None:
    parser = argparse.ArgumentParser(description="Inkwise operator tools")
    parser.add_argument("--env-file",type=Path,help="Explicit private configuration file; never printed")
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("setup-local")
    setup.add_argument("--directory", type=Path, default=Path("data/private/local-demo"))
    setup.add_argument("--teaching", type=Path, default=Path("teaching"))
    check = sub.add_parser("doctor")
    check.add_argument("--connect", action="store_true")
    teaching = sub.add_parser("teaching-check")
    teaching.add_argument("--directory", type=Path, default=Path("teaching"))
    teaching.add_argument("--json", action="store_true", help="Content-free synthetic readiness with explicit external gates")
    local = sub.add_parser("local")
    local.add_argument("--port", type=int, choices=range(1024, 65536), default=8766, metavar="PORT")
    cycle=sub.add_parser("learning-cycle")
    cycle.add_argument('--tenant',required=True)
    evolution=sub.add_parser('evolution-worker')
    evolution.add_argument('--tenant',required=True)
    experiment=sub.add_parser('experiment-create')
    experiment.add_argument('--tenant',required=True);experiment.add_argument('--file',type=Path,required=True);experiment.add_argument('--actor',required=True)
    experiment_eval=sub.add_parser('experiment-evaluate')
    experiment_eval.add_argument('--tenant',required=True);experiment_eval.add_argument('--id',required=True)
    language=sub.add_parser("attach-language-policy")
    for name in ('tenant','rubric','release','submitted-by','approved-by','source'):language.add_argument('--'+name,required=True)
    language.add_argument('--file',type=Path,required=True)
    reference = sub.add_parser("reference-index")
    reference.add_argument("--tenant",required=True)
    initialization = sub.add_parser("init-db")
    initialization.add_argument('--backup-file', type=Path, help='Private exact stopped-database copy; required for local additive upgrades')
    upgrade = sub.add_parser('upgrade-check', help='Read-only local SQLite schema, backup, space and downgrade check')
    upgrade.add_argument('--backup-file', type=Path)
    upgrade.add_argument('--target-data-feature', action='append')
    sub.add_parser("storage-status", help="Read bounded cache/original-image metadata and disk headroom")
    seed = sub.add_parser("seed-demo")
    seed.add_argument("--tenant", required=True)
    account = sub.add_parser("provision")
    account.add_argument("--tenant", required=True)
    account.add_argument("--classroom", action="append", default=[])
    account.add_argument("--role", choices=["teacher", "reviewer", "admin"], default="teacher")
    account.add_argument("--output", type=Path, required=True)
    revoke = sub.add_parser("revoke")
    revoke.add_argument("--user-id", required=True)
    server = sub.add_parser("serve")
    server.add_argument("--host", default="127.0.0.1")
    server.add_argument("--port", type=int, default=8766)
    sub.add_parser("worker")
    sub.add_parser("chunk-worker")
    words=sub.add_parser('lexicon-build')
    words.add_argument('--tenant',required=True)
    words.add_argument('--source',type=Path,required=True)
    words.add_argument('--model-revision',required=True)
    words.add_argument('--submitted-by',required=True)
    words.add_argument('--approved-by',required=True)
    rule_import=sub.add_parser("import-rubric")
    rule_import.add_argument("--tenant",required=True)
    rule_import.add_argument("--file",type=Path,required=True)
    rule_import.add_argument("--submitted-by",required=True)
    rule_import.add_argument("--approved-by",required=True)
    rule_import.add_argument("--source",required=True)
    reconcile = sub.add_parser("reconcile-call")
    reconcile.add_argument("--receipt-id", required=True)
    reconcile.add_argument("--action", choices=["recover", "retry"], required=True)
    reconcile.add_argument("--actor", required=True)
    reconcile.add_argument("--reason", required=True)
    reconcile.add_argument("--provider-checked", action="store_true", required=True)
    reconcile.add_argument("--result-file", type=Path)
    export = sub.add_parser("knowledge-export")
    export.add_argument("--tenant", required=True)
    export.add_argument("--release", required=True)
    index = sub.add_parser("knowledge-index")
    index.add_argument("--bundle", type=Path, required=True)
    activate = sub.add_parser("knowledge-activate")
    activate.add_argument("--bundle", type=Path, required=True)
    activate.add_argument("--evaluation", type=Path, required=True)
    recovery = sub.add_parser("knowledge-recover")
    recovery.add_argument("--tenant", required=True)
    rollback = sub.add_parser("knowledge-rollback")
    rollback.add_argument("--tenant", required=True)
    rollback.add_argument("--release", required=True)
    suite=sub.add_parser("evaluate-suite")
    suite.add_argument("--source",type=Path,required=True)
    suite.add_argument("--output",type=Path,required=True)
    fit=sub.add_parser("calibration-fit")
    fit.add_argument("--source",type=Path,required=True)
    fit.add_argument("--output",type=Path,required=True)
    calibration=sub.add_parser("calibration-publish")
    calibration.add_argument("--tenant",required=True)
    calibration.add_argument("--profile",type=Path,required=True)
    calibration.add_argument("--reviewer",required=True)
    evaluate = sub.add_parser("evaluate")
    evaluate.add_argument("--labels", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    settings = Settings(_env_file=args.env_file) if args.env_file else Settings()
    if args.command == "setup-local":
        from inkwise.setup import setup_local
        result = setup_local(args.directory, args.teaching)
        print("Isolated local workspace prepared:", result["rubrics"], "draft rubrics")
        print("Access files and .env saved privately; no services started or external words activated")
    elif args.command == "local":
        from inkwise.setup import run_local
        if args.env_file is None:
            raise SystemExit("LOCAL_ENV_FILE_REQUIRED")
        raise SystemExit(run_local(args.env_file, args.port))
    elif args.command == "doctor":
        from inkwise.setup import doctor
        report = doctor(settings, connect=args.connect)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        if not report["ready"]:
            raise SystemExit(1)
    elif args.command == "teaching-check":
        from inkwise.setup import teaching_readiness
        report = teaching_readiness(args.directory)
        if args.json:
            print(json.dumps(report, ensure_ascii=False, indent=2))
        else:
            print("Validated synthetic drafts:", report["rubricCount"], "; school acceptance: pending")
    elif args.command == 'learning-cycle':
        from inkwise.evolution import learning_cycle
        print(json.dumps(learning_cycle(settings,args.tenant),ensure_ascii=False))
    elif args.command == 'evolution-worker':
        import asyncio
        from inkwise.evolution import run_evolution_worker
        asyncio.run(run_evolution_worker(settings,args.tenant))
    elif args.command == 'experiment-create':
        from inkwise.evolution import create_experiment
        from inkwise.knowledge import read_bundle_text
        record=create_experiment(settings,args.tenant,json.loads(read_bundle_text(args.file)),args.actor)
        print('Approved weight experiment created:',record['id'])
    elif args.command == 'experiment-evaluate':
        from inkwise.evolution import evaluate_experiment
        print(json.dumps(evaluate_experiment(settings,args.tenant,args.id),ensure_ascii=False))
    elif args.command == "attach-language-policy":
        from inkwise.language import attach_language_policy
        attach_language_policy(settings,args.tenant,args.rubric,args.file,args.release,args.submitted_by,args.approved_by,args.source)
        print("Language policy frozen in a new independently approved rubric release")
    elif args.command == "reference-index":
        import asyncio
        from inkwise.reference import index_references
        result=asyncio.run(index_references(settings,args.tenant))
        print("Indexed public curriculum references:",result["indexedReferences"])
    elif args.command == "storage-status":
        from inkwise.cache_storage import storage_status
        print(json.dumps(storage_status(settings)))
    elif args.command == 'upgrade-check':
        from inkwise.local_upgrade import inspect_upgrade
        report = inspect_upgrade(settings, backup=args.backup_file, target_features=args.target_data_feature)
        print(json.dumps(report, ensure_ascii=False))
        if report['status'] != 'compatible':
            raise SystemExit(3 if report['status'] == 'blocked' else 2)
    elif args.command == "init-db":
        if settings.database_url.startswith('sqlite:///'):
            from inkwise.local_upgrade import inspect_upgrade
            report = inspect_upgrade(settings, backup=args.backup_file)
            if not report['canInitialize']:
                print(json.dumps(report, ensure_ascii=False))
                raise SystemExit('LOCAL_UPGRADE_BLOCKED; stop the workspace and verify a private backup with upgrade-check')
        initialize_database(settings.database_url)
        print("Database schema initialized")
    elif args.command == "seed-demo":
        seed_demo(settings, args.tenant)
        print("Synthetic rule release installed")
    elif args.command == "provision":
        print("Provisioned identity:", provision(settings, args.tenant, args.classroom, args.role, args.output))
        print("Access token saved to the requested private file; not printed")
    elif args.command == "revoke":
        engine, sessions = create_session_factory(settings.database_url)
        with sessions.begin() as db:
            identity = db.get(Identity, args.user_id)
            if identity is None:
                raise SystemExit("Identity not found")
            identity.active = False
        engine.dispose()
        print("Identity revoked")
    elif args.command == "serve":
        import uvicorn
        from inkwise.api import create_app
        uvicorn.run(create_app(settings), host=args.host, port=args.port, access_log=False)
    elif args.command == "worker":
        import asyncio
        from inkwise.worker import run_worker
        asyncio.run(run_worker(settings))
    elif args.command == "chunk-worker":
        import asyncio
        from inkwise.chunks import run_chunk_worker
        asyncio.run(run_chunk_worker(settings))
    elif args.command == "knowledge-export":
        from inkwise.knowledge import export_bundle
        print("Exported bundle:", export_bundle(settings, args.tenant, args.release))
    elif args.command == "import-rubric":
        from inkwise.knowledge import import_rubric
        import_rubric(settings,args.tenant,args.file,submitted_by=args.submitted_by,
                      approved_by=args.approved_by,source=args.source)
        print("Independently approved rule file imported; graph/case indexing is a separate operation")
    elif args.command=='lexicon-build':
        import asyncio
        from inkwise.lexicon import build_reviewed_lexicon
        result=asyncio.run(build_reviewed_lexicon(settings,args.tenant,args.source,args.model_revision,args.submitted_by,args.approved_by))
        print('Lexicon entries:',result['entries'],'reused embeddings:',result['reusedEmbeddings'],'new embeddings:',result['newEmbeddings'])
    elif args.command == "reconcile-call":
        from inkwise.recovery import resolve_receipt
        engine, sessions = create_session_factory(settings.database_url)
        try:
            resolve_receipt(sessions,args.receipt_id,action=args.action,actor=args.actor,reason=args.reason,
                provider_checked=args.provider_checked,
                recovered_result=json.loads(args.result_file.read_text()) if args.result_file else None)
        finally:
            engine.dispose()
        print("Provider result reconciled; job retry remains an explicit action")
    elif args.command == "knowledge-index":
        import asyncio
        from inkwise.knowledge import index_bundle
        result = asyncio.run(index_bundle(settings, args.bundle))
        print("Prepared inactive release:", result["release"], "cases:", result["indexedCases"])
    elif args.command == "knowledge-activate":
        from inkwise.knowledge import activate_bundle
        activate_bundle(settings, args.bundle, args.evaluation)
        print("Approved knowledge release activated")
    elif args.command == "knowledge-recover":
        from inkwise.knowledge import recover_release
        recover_release(settings, args.tenant)
        print("Interrupted knowledge switch recovered from its database commit marker")
    elif args.command == "knowledge-rollback":
        from inkwise.knowledge import rollback_release
        rollback_release(settings, args.tenant, args.release)
        print("Previous complete knowledge release restored")
    elif args.command == "evaluate-suite":
        from inkwise.benchmarks import evaluate_suite
        from inkwise.cache_storage import atomic_json
        from inkwise.knowledge import read_bundle_text
        atomic_json(args.output,evaluate_suite(json.loads(read_bundle_text(args.source))))
        print("Evaluation summaries saved without source text")
    elif args.command == "calibration-fit":
        from inkwise.calibration import fit_profile
        from inkwise.cache_storage import atomic_json
        from inkwise.knowledge import read_bundle_text
        profile=fit_profile(json.loads(read_bundle_text(args.source)))
        atomic_json(args.output,profile)
        print("Held-out calibration profile prepared; not activated")
    elif args.command == "calibration-publish":
        from inkwise.calibration import publish_profile
        publish_profile(settings,args.tenant,args.profile,args.reviewer)
        print("Scoped calibration profile published; teacher review remains required")
    elif args.command == "evaluate":
        from inkwise.evaluation import metrics, cluster_bootstrap_mae
        from inkwise.cache_storage import atomic_json
        rows = json.loads(args.labels.read_text())
        report = metrics(rows)
        report["maeConfidenceInterval"] = cluster_bootstrap_mae([r for r in rows if r["split"] == "test"])
        report["labelFileHash"] = digest(args.labels.read_text())
        atomic_json(args.output, report)
        print("Evaluation metrics saved; publication still requires independent approval")


def main() -> None:
    from inkwise.errors import DomainError
    from pydantic import ValidationError
    from sqlalchemy.exc import SQLAlchemyError
    try:
        _main()
    except DomainError as exc:
        raise SystemExit(exc.code) from None
    except (ValidationError,json.JSONDecodeError,SQLAlchemyError):
        raise SystemExit("OPERATOR_INPUT_OR_STORAGE_INVALID; no private input was printed") from None
