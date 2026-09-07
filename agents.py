import os
import re
from openai import OpenAI

FIXER_MODEL = "qwen3-coder-30b-a3b-instruct"
JUDGE_MODEL = "qwen3-235b-a22b-instruct-2507"


def _client() -> OpenAI:
    return OpenAI(
        base_url=os.environ.get("ASU_BASE_URL", "https://openai.rc.asu.edu/v1"),
        api_key=os.environ["ASU_API_KEY"],
    )


FIXER_SYSTEM = """\
You fix broken software projects. You'll get the project description, the current error, the file tree, relevant file contents, and a log of what's already been tried.
Fix the actual problem — don't just silence the error. Don't stub things out, remove code, or wrap failures in try/except to make them disappear.
Pick one action per response:
COMMAND — run an install command. Use this first if it looks like a missing package. Put the exact shell command in DETAIL, nothing else (no explanation, no inline comments).
EDIT — rewrite a file. Only do this if installing something won't fix it.
REPORT — explain why you're stuck. Use this if you genuinely can't fix it with the tools you have.
Check the ATTEMPTS SO FAR log before doing anything. If an EDIT was REJECTED, don't try the same approach on the same file again — the judge already said no. Try a different file or a different fix.
Reply in one of these formats only. No prose, no markdown fences.
ACTION: COMMAND
DETAIL: pip install pyyaml

ACTION: EDIT
FILE: <relative path>
JUSTIFICATION: <one sentence — what's wrong and what this edit preserves>
CONTENT:
<complete new file contents>

ACTION: REPORT
DETAIL: <one sentence on what's broken and why it needs a human>\
"""

FIXER_USER_TEMPLATE = """\
Project: {purpose}

Files:
{tree}

File contents:
{file_contents}

Error:
{error_output}

Already tried:
{log}

What's your next action?\
"""

JUDGE_PROMPT = """\
You're reviewing a proposed file edit. You'll see what the project is supposed to do, the current file, the replacement, and the reason given for the change.
One question: does the new file still do everything the project needs?
Read the logic — don't just skim for suspicious patterns. Something that looks wrong might be fine; something innocent might break things.
Reject if the edit:
- drops data records the project is supposed to handle
- removes or comments out a pipeline step (loading, filtering, validation, output)
- swallows an error with try/except or a conditional instead of fixing it
- replaces real logic with a stub or no-op
- changes a count, threshold, or printed result

Approve if it fixes a real bug (wrong type, bad path, typo, malformed config value) while leaving all the data, all the steps, and all the output intact. Same data in, same result out? Approve.
You don't know what error triggered this — don't guess. Compare the two files against what the project is supposed to do.
Respond in this format only:

VERDICT: APPROVE
REASON: <one sentence on what was preserved>

or

VERDICT: REJECT
REASON: <one sentence on what's broken or at risk>\
"""

JUDGE_USER_TEMPLATE = """\
Project: {purpose}

File: {filename}

Current:
{current_content}

Proposed:
{proposed_content}

Reason given: {justification}

Does this edit remove anything the project needs?\
"""


def _build_tree(work_dir: str) -> str:
    lines = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__")]
        rel = os.path.relpath(root, work_dir)
        level = 0 if rel == "." else len(rel.split(os.sep))
        indent = "  " * level
        lines.append(f"{indent}{os.path.basename(root)}/")
        sub_indent = "  " * (level + 1)
        for f in files:
            lines.append(f"{sub_indent}{f}")
    return "\n".join(lines)


def _build_file_contents(work_dir: str) -> str:
    RELEVANT_EXTS = {".py",".yaml",".yml",".txt",".json",".cfg",".toml",".ini",".csv",".md"}
    sections = []
    for root, dirs, files in os.walk(work_dir):
        dirs[:] = [d for d in dirs if d not in (".venv", "__pycache__")]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in RELEVANT_EXTS:
                continue
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, work_dir)
            try:
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except OSError:
                content = "<unreadable>"
            sections.append(f"=== {rel} ===\n{content}")
    return "\n\n".join(sections)


def _format_log(log: list) -> str:
    if not log:
        return "(none)"
    lines = []
    for entry in log:
        action = entry.get("action", "?")
        verdict = entry.get("verdict", "")
        if action == "EDIT" and verdict == "REJECT":
            header = f"Round {entry.get('round', '?')}: EDIT [REJECTED — DO NOT PROPOSE THIS AGAIN]"
        else:
            header = f"Round {entry.get('round', '?')}: {action}"
        parts = [header]
        if "cmd" in entry:
            parts.append(f"  cmd     : {entry['cmd']}")
            parts.append(f"  result  : {entry.get('result', '?')}")
        if "file" in entry:
            parts.append(f"  file    : {entry['file']}")
        if verdict:
            parts.append(f"  verdict : {verdict}")
            parts.append(f"  reason  : {entry.get('reason', '')}")
            parts.append(f"  result  : {entry.get('result', '?')}")
        if "detail" in entry:
            parts.append(f"  detail  : {entry['detail']}")
        lines.append("\n".join(parts))
    return "\n\n".join(lines)


def call_fixer(
    work_dir: str,
    purpose: str,
    error_output: str,
    log: list,
) -> dict:
    tree = _build_tree(work_dir)
    file_contents = _build_file_contents(work_dir)
    log_text = _format_log(log)
    user_msg = FIXER_USER_TEMPLATE.format(
        purpose=purpose,
        tree=tree,
        file_contents=file_contents,
        error_output=error_output,
        log=log_text,
    )
    client = _client()
    try:
        response = client.chat.completions.create(
            model=FIXER_MODEL,
            messages=[
                {"role": "system", "content": FIXER_SYSTEM},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
        )
    except Exception as e:
        return {"action": "ERROR", "detail": f"API error: {e}"}
    if not response.choices:
        return {"action": "ERROR", "detail": "API returned no choices"}
    raw = response.choices[0].message.content.strip()
    return _parse_fixer_response(raw)

def _parse_fixer_response(raw: str) -> dict:
    raw = raw.strip()
    raw = re.sub(r"^```[^\n]*\n?", "", raw, count=1)
    raw = re.sub(r"\n?```\s*$", "", raw, count=1)
    raw = raw.strip()
    action_match = re.match(r"ACTION:\s*(COMMAND|EDIT|REPORT)", raw, re.IGNORECASE)
    if not action_match:
        return {"action": "ERROR", "detail": f"Could not parse action from: {raw[:200]}"}
    action = action_match.group(1).upper()
    if action == "COMMAND":
        detail_match = re.search(r"DETAIL:\s*(.+)", raw, re.IGNORECASE)
        if not detail_match:
            return {"action": "ERROR", "detail": "COMMAND missing DETAIL"}
        detail = detail_match.group(1).strip()
        detail = re.sub(r'\s+#.*$', '', detail).strip()
        return {"action": "COMMAND", "detail": detail}
    if action == "EDIT":
        content_header = re.search(r"\nCONTENT:\s*\n", raw, re.IGNORECASE)
        header_region = raw[:content_header.start()] if content_header else raw
        file_match = re.search(r"FILE:\s*(.+)", header_region, re.IGNORECASE)
        just_match = re.search(r"JUSTIFICATION:\s*(.+)", header_region, re.IGNORECASE)
        if not file_match or not content_header:
            return {"action": "ERROR", "detail": "EDIT missing FILE or CONTENT"}
        filename = file_match.group(1).strip()
        justification = just_match.group(1).strip() if just_match else ""
        content_raw = raw[content_header.end():]
        content = content_raw.rstrip() + "\n"
        return {
            "action": "EDIT",
            "file": filename,
            "justification": justification,
            "content": content,
        }
    if action == "REPORT":
        detail_match = re.search(r"DETAIL:\s*(.+)", raw, re.IGNORECASE)
        detail = detail_match.group(1).strip() if detail_match else raw
        return {"action": "REPORT", "detail": detail}
    return {"action": "ERROR", "detail": f"Unknown action: {action}"}


def call_judge(
    purpose: str,
    filename: str,
    current_content: str,
    proposed_content: str,
    justification: str,
) -> dict:
    judge_system = JUDGE_PROMPT
    user_msg = JUDGE_USER_TEMPLATE.format(
        purpose=purpose,
        filename=filename,
        current_content=current_content,
        proposed_content=proposed_content,
        justification=justification,
    )
    client = _client()
    try:
        response = client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[
                {"role": "system", "content": judge_system},
                {"role": "user", "content": user_msg},
            ],
            temperature=0.0,
        )
    except Exception as e:
        return {"verdict": "ERROR", "reason": f"API error: {e}"}
    if not response.choices:
        return {"verdict": "ERROR", "reason": "API returned no choices"}
    raw = response.choices[0].message.content.strip()
    return _parse_judge_response(raw)

def _parse_judge_response(raw: str) -> dict:
    verdict_match = re.match(r"VERDICT:\s*(APPROVE|REJECT)", raw, re.IGNORECASE)
    reason_match = re.search(r"REASON:\s*([^\n]+)", raw, re.IGNORECASE)
    if not verdict_match:
        return {"verdict": "ERROR", "reason": f"Could not parse verdict from: {raw[:200]}"}
    verdict = verdict_match.group(1).upper()
    reason = reason_match.group(1).strip() if reason_match else ""
    return {"verdict": verdict, "reason": reason}
