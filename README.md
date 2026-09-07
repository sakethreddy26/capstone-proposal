# repo-fixer

A CLI tool that takes a broken repo, runs it, reads the error, and iterates to fix it. The fixer proposes install commands or file edits. Every file edit must be approved by a separate judge agent before it is written to disk.

Requires Python 3.10+.

## How it works

1. Copies the fixture to a temp directory so the original is never modified.
2. Installs dependencies if a `requirements.txt` is present.
3. Runs the target command and checks the exit code.
4. If it fails, asks the fixer agent for the next action (install or edit).
5. If the fixer proposes an edit, the judge agent reviews it first. The judge only sees the file before and after — not the error — so it can only ask whether the edit removes functionality.
6. Approved edits are applied. Rejected edits are logged and the fixer tries again.
7. Repeats until the command exits cleanly (and tests pass, if any are detected) or the round limit is hit.

## Files

```
fix_repo.py   CLI, repair loop, results table
harness.py    venv, install, run, apply_edit (no API calls)
agents.py     fixer + judge prompts, API calls, response parsing
fixtures/     broken repos used as test cases
results/      run output written here
```

## Setup

**1. Install dependencies**

```powershell
Set-ExecutionPolicy -ExecutionPolicy RemoteSigned -Scope CurrentUser
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

> Only needed once per machine. Skip if scripts are already enabled.

**2. Create a `.env` file** in the project root with your API credentials:

```
ASU_API_KEY=<your-key>
ASU_BASE_URL=https://openai.rc.asu.edu/v1
```

The `.env` file is loaded automatically at startup. 

## Run

```powershell
# Single fixture
python fix_repo.py --repo fixtures/repo_a

# All fixtures
python fix_repo.py --all

# Custom round limit and output path
python fix_repo.py --all --max-rounds 6 --out results/runs.json
```

## Output

Results are printed as a table in the terminal and written to `results/runs.json`.

A successful run on `fixtures/repo_a` looks like this:

```
============================================================
  Repo:    repo_a
  Command: python main.py
  Tests:   none detected (exit code only)
============================================================
  Working dir: C:\Users\...\AppData\Local\Temp\repo_fixer_...
  [install] ok
  [run] initial: ok=False

  [fixer proposes EDIT -> requirements.txt]
  ...
  round  1: COMMAND  pip install pyyaml  [ok]
  round  2: COMMAND  pip install requests  [ok]

  Result: FIXED  (rounds=2, cmds=2, edits=0, approved=0, rejected=0)
```

## Fixtures

Each fixture is a small broken repo. The tool auto-detects the run command by looking for `main.py`, `app.py`, `package.json`, `Cargo.toml`, or `go.mod`. The fixtures are intentionally broken — do not fix them manually.

## Models

Fixer: `qwen3-coder-30b-a3b-instruct`  
Judge: `qwen3-235b-a22b-instruct-2507`  

Both model IDs are constants in `agents.py`. A different model is used for each role on purpose — using the same model for both produces a judge that approves everything.
