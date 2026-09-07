import argparse
import json
import os
import sys
import glob as glob_module
from datetime import datetime
from dotenv import load_dotenv
load_dotenv()
import harness
import agents

def _read_file_safe(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError:
        return "<file not found>"



def _print_judge(judge: dict) -> None:
    verdict = judge.get("verdict", "?")
    reason = judge.get("reason", "")
    symbol = "APPROVE" if verdict == "APPROVE" else "REJECT"
    print(f"          judge  : {symbol}")
    print(f"          reason : {reason}")

def _print_round(rnd: int, action: dict, judge: dict | None, run_result: dict) -> None:
    action_type = action.get("action", "?")
    status = "ok" if run_result.get("ok") else "fail"
    if action_type == "COMMAND":
        print(f"  round {rnd:>2}: COMMAND  {action.get('detail', '')}  [{status}]")
    elif action_type == "EDIT":
        verdict = judge.get("verdict", "?") if judge else "N/A"
        applied = "applied" if verdict == "APPROVE" else "not applied"
        print(f"  round {rnd:>2}: EDIT     {action.get('file', '')}  →  {applied}  [{status}]")
    elif action_type == "REPORT":
        print(f"  round {rnd:>2}: REPORT   {action.get('detail', '')}  [{status}]")
    else:
        print(f"  round {rnd:>2}: ERROR    {action.get('detail', '')}  [{status}]")

def fix_one(fixture_path: str, max_rounds: int = 4) -> dict:
    fixture_path = os.path.abspath(fixture_path)
    name = os.path.basename(fixture_path.rstrip("/\\"))
    command = harness.detect_command(fixture_path)
    if not command:
        print(f"\n  [{name}] could not detect run command — skipping.")
        return {"repo": name, "fixed": False, "has_tests": False, "rounds": 0,
                "cmds": 0, "edits": 0, "approved": 0, "rejected": 0, "edited_files": [], "log": []}
    test_cmd = harness.detect_test_command(fixture_path)
    purpose  = harness.infer_purpose(fixture_path)
    print(f"\n{'='*60}")
    print(f"  Repo:    {name}")
    print(f"  Command: {command}")
    print(f"  Tests:   {test_cmd if test_cmd else 'none detected (exit code only)'}")
    print(f"{'='*60}")
    work_dir = harness.prepare(fixture_path, command)
    print(f"  Working dir: {work_dir}")
    install_result = harness.install(work_dir, command)
    if not install_result["ok"]:
        print(f"  [install] failed - treating as first error")
    else:
        print(f"  [install] ok")
    run_result  = harness.run_target(work_dir, command)
    test_result = None
    if run_result["ok"] and test_cmd:
        test_result = harness.run_tests(work_dir, test_cmd, command)
    print(f"  [run] initial: ok={run_result['ok']}" +
          (f", tests={test_result['ok']}" if test_result else ""))
    def combined_ok() -> bool:
        if not run_result["ok"]:
            return False
        if test_cmd and (test_result is None or not test_result["ok"]):
            return False
        return True
    log: list[dict] = []
    n_cmds = n_edits = n_approved = n_rejected = 0
    edited_files: list[str] = []
    if not run_result["ok"]:
        if not install_result["ok"]:
            current_error = (install_result["output"] + "\n" + run_result["output"]).strip()
        else:
            current_error = run_result["output"]
    elif test_cmd and test_result and not test_result["ok"]:
        current_error = test_result["output"]
    else:
        current_error = ""
    for rnd in range(1, max_rounds + 1):
        if combined_ok():
            break
        action = agents.call_fixer(
            work_dir=work_dir,
            purpose=purpose,
            error_output=current_error,
            log=log,
        )
        judge_result: dict | None = None
        install_r: dict | None = None
        if action["action"] == "COMMAND":
            cmd = action["detail"]
            n_cmds += 1
            install_r = harness.run_install_cmd(work_dir, cmd, command)
            outcome = "ok" if install_r["ok"] else "failed"
            log.append({"round": rnd, "action": "COMMAND", "cmd": cmd, "result": outcome})
            current_error = install_r["output"] if not install_r["ok"] else ""
        elif action["action"] == "EDIT":
            filename        = action["file"]
            justification   = action["justification"]
            proposed        = action["content"]
            current_content = _read_file_safe(os.path.join(work_dir, filename))
            n_edits += 1

            print(f"\n  [fixer proposes EDIT -> {filename}]")
            print(f"          justify: {justification}")
            judge_result = agents.call_judge(
                purpose=purpose,
                filename=filename,
                current_content=current_content,
                proposed_content=proposed,
                justification=justification,
            )

            _print_judge(judge_result)
            if judge_result["verdict"] == "APPROVE":
                n_approved += 1
                harness.apply_edit(work_dir, filename, proposed)
                edited_files.append(filename)
                result_str = "applied"
            else:
                n_rejected += 1
                result_str = "not applied"
            log.append({
                "round": rnd, "action": "EDIT", "file": filename,
                "verdict": judge_result["verdict"], "reason": judge_result["reason"],
                "result": result_str,
            })

        elif action["action"] == "REPORT":
            log.append({"round": rnd, "action": "REPORT", "detail": action["detail"]})
            print(f" round {rnd:>2}: REPORT {action.get('detail', '')}")
            break

        else:
            log.append({"round": rnd, "action": "ERROR","detail": action.get("detail", "unknown parse error")})
            print(f"round {rnd:>2}: ERROR {action.get('detail', '')}")
            break

        run_result  = harness.run_target(work_dir, command)
        test_result = None
        if run_result["ok"] and test_cmd:
            test_result = harness.run_tests(work_dir, test_cmd, command)

        if not run_result["ok"]:
            if install_r is not None and not install_r["ok"]:
                current_error = (install_r["output"] + "\n" + run_result["output"]).strip()
            else:
                current_error = run_result["output"]
        elif test_cmd and test_result and not test_result["ok"]:
            current_error = test_result["output"]
        else:
            current_error = ""

        _print_round(rnd, action, judge_result, run_result)

    fixed = combined_ok()
    rounds_used = len(log)
    status = "FIXED" if fixed else "NOT FIXED"
    print(f"\n  Result: {status}  (rounds={rounds_used}, cmds={n_cmds}, "f"edits={n_edits}, approved={n_approved}, rejected={n_rejected})")
    if edited_files:
        print(f"  Edited files: {', '.join(edited_files)}")
    return {
        "repo": name,
        "fixed": fixed,
        "has_tests": test_cmd is not None,
        "rounds": rounds_used,
        "cmds": n_cmds,
        "edits": n_edits,
        "approved": n_approved,
        "rejected": n_rejected,
        "edited_files": edited_files,
        "log": log,
    }

def print_table(results: list[dict]) -> None:
    header = (
        f"{'repo':<12} {'fixed':<7} {'tests':<7} {'rounds':<8} {'cmds':<6} "
        f"{'edits':<7} {'appr':<6} {'rej':<5} {'edited files'}"
    )
    sep = "-" * max(len(header), 80)
    print(f"\n{sep}")
    print(header)
    print(sep)
    for r in results:
        files_str = ", ".join(r.get("edited_files", [])) or "-"
        row = (
            f"{r['repo']:<12} "
            f"{'yes' if r['fixed'] else 'no':<7} "
            f"{'yes' if r.get('has_tests') else 'no':<7} "
            f"{r['rounds']:<8} "
            f"{r['cmds']:<6} "
            f"{r['edits']:<7} "
            f"{r['approved']:<6} "
            f"{r['rejected']:<5} "
            f"{files_str}"
        )
        print(row)
    print(sep)

def discover_fixtures(fixtures_dir: str = "fixtures") -> list[str]:
    base = os.path.abspath(fixtures_dir)
    return sorted(
        p for p in glob_module.glob(os.path.join(base, "*/"))
        if os.path.isdir(p) and harness.detect_command(p) is not None
    )

def _save_results(results: list[dict], out_path: str) -> None:
    out_dir = os.path.dirname(os.path.abspath(out_path))
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(out_path)
    archive_path = f"{base}_{ts}{ext}"
    try:
        with open(archive_path, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
    except OSError:
        print(f"Warning: could not write archive copy to {archive_path}")
    print(f"\nResults written to : {out_path}")
    print(f"Archived copy      : {archive_path}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Advisory repo-repair agent with judge approval (v0)."
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--repo", help="Path to a single fixture directory")
    group.add_argument("--all",  action="store_true", help="Run all fixtures")
    parser.add_argument("--max-rounds", type=int, default=4,help="Maximum repair rounds per fixture (default: 4)")
    parser.add_argument("--out", default="results/runs.json",help="Output JSON path (default: results/runs.json)")
    parser.add_argument("--fixtures-dir", default="fixtures",help="Directory containing fixture repos (default: fixtures)")
    args = parser.parse_args()

    if args.all:
        fixture_paths = discover_fixtures(args.fixtures_dir)
        if not fixture_paths:
            print(f"No recognizable repos found under '{args.fixtures_dir}'.")
            sys.exit(1)
    else:
        fixture_paths = [args.repo]
    results = []
    for fp in fixture_paths:
        result = fix_one(fp, max_rounds=args.max_rounds)
        results.append(result)
    _save_results(results, args.out)
    print_table(results)

if __name__ == "__main__":
    main()
