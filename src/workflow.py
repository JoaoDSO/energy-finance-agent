"""Source tools and the first real agent stage; no model API client."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
from urllib.parse import urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]


def case_root(case_id=None):
    if case_id is None:
        return ROOT
    if not re.fullmatch(r"[a-z0-9_]+", case_id):
        raise ValueError("Case ID must contain only lowercase letters, digits and underscores")
    selected = ROOT / "cases" / case_id
    if not (selected / "config/sources.json").is_file():
        raise ValueError("Unknown case configuration")
    return selected


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def sha256(path):
    with Path(path).open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def validate_source(source, allowed_hosts=None):
    parsed = urlparse(source["url"])
    hosts = set(allowed_hosts or ("cdn.orsted.com",))
    if parsed.scheme != "https" or parsed.hostname not in hosts or parsed.username or parsed.password:
        raise ValueError("Source must be on the configured official HTTPS host")
    if not re.fullmatch(r"[a-z0-9_]+", source["id"]):
        raise ValueError("Invalid source ID")
    if source["filename"] != source["id"] + ".pdf":
        raise ValueError("Filename must match source ID and use PDF format")


def fetch_sources(root=ROOT):
    config = read_json(root / "config/sources.json")
    allowed_hosts = config.get("allowed_hosts", ["cdn.orsted.com"])
    manifest_path = root / "data/manifest.json"
    manifest = read_json(manifest_path) if manifest_path.exists() else {"sources": {}}
    for source in config["sources"]:
        validate_source(source, allowed_hosts)
        target = root / "data/raw" / source["filename"]
        existing = manifest["sources"].get(source["id"], {})
        if target.exists():
            if existing.get("url") != source["url"] or existing.get("sha256") != sha256(target):
                raise ValueError(f"Unverified existing source: {source['id']}; preserve and inspect it")
            print(f"Reused verified source: {source['id']}")
            continue
        request = Request(source["url"], headers={"User-Agent": "EnergyFinanceAgent/0.1 personal-research"})
        with urlopen(request, timeout=90) as response:
            if urlparse(response.url).hostname not in allowed_hosts:
                raise ValueError("Unexpected download destination")
            content = response.read(60 * 1024 * 1024 + 1)
        if len(content) > 60 * 1024 * 1024 or not content.startswith(b"%PDF-"):
            raise ValueError("Download is not a supported-size PDF")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        manifest["sources"][source["id"]] = {
            **source, "retrieved_at": utc_now(), "sha256": sha256(target), "bytes": len(content)
        }
        write_json(manifest_path, manifest)
        print(f"Downloaded {source['id']}: {len(content):,} bytes")


def index_sources(root=ROOT):
    from pypdf import PdfReader
    manifest = read_json(root / "data/manifest.json")
    output = root / "data/pages.jsonl"
    records = []
    config = read_json(root / "config/sources.json")
    for source in config["sources"]:
        validate_source(source, config.get("allowed_hosts", ["cdn.orsted.com"]))
        entry = manifest["sources"][source["id"]]
        path = root / "data/raw" / source["filename"]
        if entry["url"] != source["url"] or sha256(path) != entry["sha256"]:
            raise ValueError(f"Source changed: {source['id']}")
        reader = PdfReader(path)
        for page_num, page in enumerate(reader.pages, 1):
            records.append({"source_id": source["id"], "page": page_num,
                            "sha256": entry["sha256"], "text": page.extract_text() or ""})
        print(f"Indexed {source['id']}: {len(reader.pages)} pages")
    output.write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in records), encoding="utf-8")


def pages(root=ROOT):
    path = root / "data/pages.jsonl"
    if not path.exists():
        raise ValueError("Run fetch and index before searching or starting research")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def normalise(text):
    return " ".join(text.split())


def search(query, limit=8, root=ROOT, source_id=None):
    needle = query.casefold().strip()
    if not needle:
        raise ValueError("Search query cannot be empty")
    matches = []
    for item in pages(root):
        if source_id is not None and item["source_id"] != source_id:
            continue
        compact = normalise(item["text"])
        position = compact.casefold().find(needle)
        if position >= 0:
            matches.append({"source_id": item["source_id"], "page": item["page"],
                            "excerpt": compact[max(0, position - 140):position + 550]})
    return matches[:limit]


def check_evidence(result, root=ROOT):
    errors = []
    evidence = {(p["source_id"], p["page"]): normalise(p["text"]) for p in pages(root)}
    findings = result.get("findings", [])
    if not isinstance(findings, list) or len(findings) < 3:
        return {"passed": False, "errors": ["At least three findings required"]}
    if not isinstance(result.get("gaps"), list) or not isinstance(result.get("next_checks"), list):
        errors.append("Missing structured gaps or next_checks")
    if not isinstance(result.get("question"), str) or not result["question"].strip():
        errors.append("Missing research question")
    for number, finding in enumerate(findings, 1):
        if not isinstance(finding, dict):
            errors.append(f"Finding {number}: not an object")
            continue
        if not all(isinstance(finding.get(k), str) for k in ("claim", "source_id", "excerpt", "caveat")):
            errors.append(f"Finding {number}: invalid fields")
            continue
        if not finding["claim"].strip() or type(finding.get("page")) is not int:
            errors.append(f"Finding {number}: missing claim or invalid page")
            continue
        page = evidence.get((finding["source_id"], finding["page"]))
        excerpt = normalise(finding["excerpt"])
        if page is None:
            errors.append(f"Finding {number}: unknown source/page")
        elif len(excerpt) < 25 or excerpt not in page:
            errors.append(f"Finding {number}: excerpt not found on cited page")
    return {"passed": not errors, "errors": errors,
            "limitation": "Checks source/page and excerpt existence, not whether a claim follows logically or a table was interpreted correctly."}


def subscription_env():
    env = os.environ.copy()
    for name in ("OPENAI_API_KEY", "CODEX_API_KEY", "OPENAI_BASE_URL", "OPENAI_API_BASE"):
        env.pop(name, None)
    return env


def check_research_trace(path, research):
    events = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    calls = [event["item"] for event in events
             if event.get("type") == "item.completed"
             and event.get("item", {}).get("type") == "mcp_tool_call"
             and event["item"].get("server") == "energy_finance"
             and event["item"].get("status") == "completed"
             and not event["item"].get("error")]
    reads = {(item["arguments"].get("source_id"), item["arguments"].get("page"))
             for item in calls if item.get("tool") == "read_report_page"}
    errors = []
    for finding in research.get("findings", []):
        if (finding["source_id"], finding["page"]) not in reads:
            errors.append("A finding cites a page that was not read through MCP in this run")
    submissions = [item["result"].get("structured_content", {}) for item in calls
                   if item.get("tool") == "submit_research_pack" and isinstance(item.get("result"), dict)]
    saved = [submission for submission in submissions if submission.get("saved") is True]
    if not saved:
        errors.append("No successful evidence-pack handoff was recorded")
    return {"passed": not errors, "errors": errors, "successful_mcp_calls": len(calls),
            "distinct_pages_read": len(reads), "handoffs": [item.get("research_path") for item in saved]}


def audit_run(run_dir, root=ROOT):
    run_dir = Path(run_dir).resolve()
    if not run_dir.is_relative_to((root / "runs").resolve()):
        raise ValueError("Audit target must be a project run directory")
    result = read_json(run_dir / "research.json")
    evidence = check_evidence(result, root)
    trace = check_research_trace(run_dir / "events.jsonl", result) if evidence["passed"] else {"passed": False, "errors": ["Evidence validation failed first"]}
    write_json(run_dir / "evidence_check.json", evidence)
    write_json(run_dir / "trace_check.json", trace)
    passed = evidence["passed"] and trace["passed"]
    write_json(run_dir / "status.json", {"stage": "research", "model_called": True,
               "status": "evidence_checked" if passed else "needs_correction"})
    return {"passed": passed, "evidence": evidence, "trace": trace}


def find_codex():
    executable = shutil.which("codex")
    if executable:
        return executable
    extensions = Path.home() / ".vscode/extensions"
    candidates = [p for p in extensions.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe") if p.is_file()]
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime))
    raise ValueError("Codex CLI was not found on PATH or in the VS Code extension")


def research_command(executable, root, run_dir, stage="research"):
    if stage not in ("research", "analysis", "review"):
        raise ValueError("Unknown agent stage")
    python = ROOT / ".venv/Scripts/python.exe" if os.name == "nt" else ROOT / ".venv/bin/python"
    if not python.is_file():
        raise ValueError("Create the project .venv and install requirements before starting the MCP agent")
    args = [str(ROOT / "src/mcp_server.py"), "--client", "codex"]
    if root != ROOT:
        args.extend(["--case", root.name])
    if stage in ("analysis", "review"):
        args.extend(["--stage", stage, "--run-dir", str(run_dir)])
    return [executable, "exec", "--ignore-user-config", "--sandbox", "read-only",
            "--skip-git-repo-check", "--ephemeral", "--json",
            "-c", "mcp_servers.energy_finance.command=" + json.dumps(str(python)),
            "-c", "mcp_servers.energy_finance.args=" + json.dumps(args),
            "-c", "mcp_servers.energy_finance.cwd=" + json.dumps(str(root)),
            "-c", 'mcp_servers.energy_finance.default_tools_approval_mode="approve"',
            "-c", "mcp_servers.energy_finance.required=true",
            "-c", "mcp_servers.energy_finance.startup_timeout_sec=30",
            "--output-schema", str((root / f"config/{stage}.schema.json") if (root / f"config/{stage}.schema.json").is_file() else (ROOT / f"config/{stage}.schema.json")),
            "--output-last-message", str(run_dir / f"{stage}.json"), "-"]


def research(preview=False, root=ROOT):
    # Validating local inputs costs no model tokens.
    indexed = pages(root)
    source_ids = {item["source_id"] for item in indexed}
    expected = {item["id"] for item in read_json(root / "config/sources.json")["sources"]}
    if source_ids != expected:
        raise ValueError("Source index does not match the configured evidence set")
    run_dir = root / "runs" / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    run_dir.mkdir(parents=True)
    prompt = ((root / "agents/research.md").read_text(encoding="utf-8")
              + "\n\nResearch configuration:\n" + json.dumps(read_json(root / "config/sources.json"))
              + "\nReturn your final response using the supplied JSON schema.")
    (run_dir / "research_prompt.md").write_text(prompt, encoding="utf-8")
    write_json(run_dir / "status.json", {"stage": "research", "status": "preview", "model_called": False})
    if preview:
        print(f"Prepared research prompt; no model called: {run_dir}")
        return run_dir
    executable = find_codex()
    auth = subprocess.run([executable, "login", "status"], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=subscription_env(), timeout=30)
    if auth.returncode or "chatgpt" not in (auth.stdout + auth.stderr).casefold():
        write_json(run_dir / "status.json", {"stage": "research", "status": "awaiting_chatgpt_login", "model_called": False})
        raise ValueError("Sign in using codex login and choose ChatGPT; no API fallback is enabled")
    command = research_command(executable, root, run_dir)
    write_json(run_dir / "status.json", {"stage": "research", "status": "running", "model_called": True})
    try:
        with (run_dir / "events.jsonl").open("w", encoding="utf-8") as events, \
                (run_dir / "stderr.txt").open("w", encoding="utf-8") as stderr:
            process = subprocess.run(command, input=prompt, text=True, encoding="utf-8", cwd=root,
                                     env=subscription_env(), stdout=events, stderr=stderr, timeout=900)
        if process.returncode:
            raise ValueError(f"Codex failed; inspect local run logs in {run_dir}")
        passed = audit_run(run_dir, root)["passed"]
        print(f"Research completed; evidence and trace checks passed={passed}; run={run_dir}")
        if not passed:
            raise ValueError("Research needs correction; later stages were not started")
        return run_dir
    except Exception as error:
        status = read_json(run_dir / "status.json")
        if status["status"] == "running":
            write_json(run_dir / "status.json", {"stage": "research", "status": "failed",
                                                  "model_called": True, "error": str(error)})
        raise


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", help="Use a separately configured case under cases/")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("fetch")
    commands.add_parser("index")
    find = commands.add_parser("search")
    find.add_argument("query")
    find.add_argument("--limit", type=int, default=8, choices=range(1, 31))
    find.add_argument("--source", help="Restrict results to one source ID")
    page = commands.add_parser("page")
    page.add_argument("source_id")
    page.add_argument("number", type=int)
    agent = commands.add_parser("research")
    agent.add_argument("--preview", action="store_true")
    validate = commands.add_parser("check")
    validate.add_argument("result_file", type=Path)
    audit = commands.add_parser("audit")
    audit.add_argument("run_dir", type=Path)
    analysis = commands.add_parser("analyze")
    analysis.add_argument("--research-run", type=Path, help="Defaults to the latest research run with valid evidence and tool trace")
    analysis.add_argument("--preview", action="store_true")
    pipeline = commands.add_parser("pipeline")
    pipeline.add_argument("--research-run", type=Path, help="Reuse an existing validated research run instead of starting new research")
    pipeline.add_argument("--with-review", action="store_true", help="Review analysis with at most one correction and a fresh re-review")
    review = commands.add_parser("review")
    review.add_argument("--analysis-run", type=Path, required=True)
    review.add_argument("--preview", action="store_true")
    review_audit = commands.add_parser("audit-review")
    review_audit.add_argument("run_dir", type=Path)
    human = commands.add_parser("revise-human")
    human.add_argument("--analysis-run", type=Path, required=True)
    human.add_argument("--review-run", type=Path, required=True)
    human.add_argument("--request-file", type=Path, required=True)
    human.add_argument("--preview", action="store_true")
    run = commands.add_parser("run", help="Recheck/reuse the current package, or explicitly start fresh agent stages")
    run.add_argument("--package", type=Path)
    run.add_argument("--fresh", action="store_true")
    run.add_argument("--research-client", choices=("claude_desktop", "codex"))
    run.add_argument("--research-run", type=Path)
    run.add_argument("--resume", type=Path)
    status = commands.add_parser("status")
    status.add_argument("--run", type=Path)
    status.add_argument("--json", action="store_true", help="Show the complete machine-readable audit record")
    decide = commands.add_parser("decide")
    decide.add_argument("--run", type=Path, required=True)
    decide.add_argument("--decision", choices=("approve_historical", "approve_with_conditions", "request_changes", "reject"), required=True)
    decide.add_argument("--scope", choices=("historical", "expanded"), default="historical")
    decide.add_argument("--expected-sha", required=True)
    decide.add_argument("--reviewer", required=True)
    decide.add_argument("--note", required=True)
    decide.add_argument("--condition", action="append", default=[])
    inspect_analysis = commands.add_parser("audit-analysis")
    inspect_analysis.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    try:
        root = case_root(args.case)
        if args.command == "fetch":
            fetch_sources(root)
        elif args.command == "index":
            index_sources(root)
        elif args.command == "search":
            print(json.dumps(search(args.query, args.limit, root=root, source_id=args.source), ensure_ascii=False, indent=2))
        elif args.command == "page":
            matches = [p for p in pages(root) if p["source_id"] == args.source_id and p["page"] == args.number]
            if not matches:
                raise ValueError("Unknown source/page")
            print(json.dumps(matches[0], ensure_ascii=False, indent=2))
        elif args.command == "research":
            research(args.preview, root=root)
        elif args.command == "check":
            checked = check_evidence(read_json(args.result_file), root)
            print(json.dumps(checked, indent=2))
            return 0 if checked["passed"] else 1
        elif args.command == "audit":
            checked = audit_run(args.run_dir, root)
            print(json.dumps(checked, indent=2))
            return 0 if checked["passed"] else 1
        elif args.command in ("analyze", "pipeline"):
            from analysis_stage import analyze
            selected = args.research_run
            if args.command == "pipeline" and selected is None:
                selected = research(root=root)
            analysis_run = analyze(selected, preview=getattr(args, "preview", False), root=root)
            if getattr(args, "with_review", False):
                from review_stage import review_cycle
                review_cycle(analysis_run, root=root)
        elif args.command == "review":
            from review_stage import review_cycle
            review_cycle(args.analysis_run, preview=args.preview, root=root)
        elif args.command == "revise-human":
            from human_revision import human_revision
            human_revision(args.analysis_run, args.review_run, args.request_file, preview=args.preview, root=root)
        elif args.command == "run":
            from run_control import run
            if not args.fresh and args.resume is None and (args.research_run or args.research_client):
                raise ValueError("Research selection requires --fresh or --resume")
            if args.package and args.fresh:
                raise ValueError("Choose package reuse or a fresh run, not both")
            run(args.package, args.fresh, args.research_client, args.research_run, args.resume, root=root)
        elif args.command == "status":
            from decisions import readable_status, status
            item = status(args.run, root=root)
            print(json.dumps(item, ensure_ascii=False, indent=2) if args.json else readable_status(item))
        elif args.command == "decide":
            from decisions import record
            record(args.run, args.decision, args.scope, args.expected_sha, args.reviewer, args.note, args.condition, root=root)
        elif args.command == "audit-review":
            from review_stage import audit_review
            checked = audit_review(args.run_dir, root=root)
            print(json.dumps(checked, indent=2))
            return 0 if checked["passed"] else 1
        elif args.command == "audit-analysis":
            from analysis_stage import audit_analysis
            checked = audit_analysis(args.run_dir, root=root)
            print(json.dumps(checked, indent=2))
            return 0 if checked["passed"] else 1
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as error:
        print(f"Stopped: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
