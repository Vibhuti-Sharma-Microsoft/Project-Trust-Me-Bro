from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from typing import Sequence

from .bundle import build_bundle
from .config import load_config
from .corpus import prepare_corpus
from .demo import create_demo
from .executor import evaluate_case
from .imports import load_manifest, write_json
from .models import BatchResult
from .report import render_report, serve_reports
from .time_utils import utc_now


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Local response-level SRE evidence scoring; never writes to IcM.")
    commands = root.add_subparsers(dest="command", required=True)
    demo = commands.add_parser("demo", help="Generate ten clearly labeled synthetic replay cases")
    demo.add_argument("--directory", type=Path, default=Path("data/demo"))
    prepare = commands.add_parser("prepare-corpus", help="Prepare a private incident collection checklist and read-only KQL")
    prepare.add_argument("--incidents-file", type=Path, required=True)
    prepare.add_argument("--directory", type=Path, required=True)
    validate = commands.add_parser("validate", help="Validate local evidence without model/network calls")
    validate.add_argument("--manifest", type=Path, required=True)
    evaluate = commands.add_parser("evaluate", help="Evaluate selected local cases and produce JSON/HTML")
    evaluate.add_argument("--manifest", type=Path, required=True)
    evaluate.add_argument("--config", type=Path)
    evaluate.add_argument("--case", action="append", default=[])
    evaluate.add_argument("--judge-mode", choices=["replay", "live"], default="replay")
    evaluate.add_argument("--cache-directory", type=Path, default=Path(".cache"))
    evaluate.add_argument("--out", type=Path, default=Path("out"))
    evaluate.add_argument("--run-id")
    render = commands.add_parser("render", help="Render persisted results without rerunning judges")
    render.add_argument("--results", type=Path, required=True)
    render.add_argument("--out", type=Path)
    serve = commands.add_parser("serve", help="Serve only a generated report directory on loopback")
    serve.add_argument("--directory", type=Path, required=True)
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8080)
    return root


def main(argv: Sequence[str] | None = None) -> int:
    args = parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    try:
        if args.command == "demo":
            manifest = create_demo(args.directory)
            print(f"Created SYNTHETIC corpus: {manifest}")
            return 0
        if args.command == "prepare-corpus":
            path = prepare_corpus(args.incidents_file, args.directory)
            print(f"Collection plan: {path}\nState: AWAITING_EXPORTS (not an executable evaluation manifest)")
            return 0
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise ValueError("Port must be between 1 and 65535")
            print(f"Report URL: http://{args.host}:{args.port}", flush=True)
            serve_reports(args.directory, host=args.host, port=args.port)
            return 0
        if args.command == "render":
            batch = BatchResult.model_validate_json(args.results.read_text(encoding="utf-8"))
            print(render_report(batch, args.out or args.results.parent))
            return 0
        manifest = load_manifest(args.manifest)
        root = args.manifest.resolve().parent
        if args.command == "validate":
            outcomes = []
            for case in manifest.cases:
                try:
                    bundle = build_bundle(case, root)
                    outcomes.append({"case": case.id, "status": "VALID", "warnings": bundle.warnings,
                                     "tool_calls": len(bundle.calls), "todo_available": bundle.todo is not None})
                except (ValueError, OSError, UnicodeError) as exc:
                    outcomes.append({"case": case.id, "status": "IMPORT_ERROR", "error": str(exc)})
            print(json.dumps({"target_real_cases": manifest.target_real_cases,
                              "actual_real_cases": sum(not case.synthetic for case in manifest.cases),
                              "cases": outcomes}, indent=2))
            return 1 if any(row["status"] != "VALID" for row in outcomes) else 0
        selected = [case for case in manifest.cases if not args.case or case.id in args.case]
        if args.case and set(args.case) - {case.id for case in selected}:
            raise ValueError("Requested case ID is not present in the manifest")
        run_id = args.run_id or f"run-{uuid.uuid4().hex[:12]}"
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", run_id):
            raise ValueError("Run ID must be a simple alphanumeric/hyphen/underscore name")
        output_dir = args.out / run_id
        if output_dir.exists():
            raise FileExistsError("Run output already exists; choose a new --run-id")
        config = load_config(args.config)
        results = [evaluate_case(case, root, config, args.cache_directory, args.judge_mode) for case in selected]
        batch = BatchResult(run_id=run_id, created_at=utc_now(), policy_version=config.policy_version,
                            policy_sha256=config.policy_sha256, weights=config.weights, target_real_cases=manifest.target_real_cases,
                            selected_real_cases=sum(not case.synthetic for case in selected), results=results)
        write_json(output_dir / "results.json", batch.model_dump(mode="json"))
        report = render_report(batch, output_dir)
        print(f"JSON: {output_dir / 'results.json'}\nHTML: {report}")
        print(f"Real cases: {batch.selected_real_cases}/{batch.target_real_cases}; synthetic cases: {sum(case.synthetic for case in selected)}")
        for result in results:
            print(f"{result.case_id}: {result.status}, score={result.score}")
        return 1 if any(result.status in {"UNSCORABLE", "JUDGE_ERROR", "IMPORT_ERROR"} for result in results) else 0
    except (ValueError, OSError) as exc:
        logging.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
