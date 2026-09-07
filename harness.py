import json
import os
import sys
import shlex
import shutil
import subprocess
import tempfile


def _truncate(text: str, max_chars: int = 2000) -> str:
    if len(text) <= max_chars:
        return text
    return "...[truncated]...\n" + text[-max_chars:]


def _venv_python(work_dir: str) -> str:
    if sys.platform == "win32":
        return os.path.join(work_dir, ".venv", "Scripts", "python.exe")
    return os.path.join(work_dir, ".venv", "bin", "python")


def _is_python(command: str) -> bool:
    try:
        tokens = shlex.split(command)
    except ValueError:
        return False
    return bool(tokens) and os.path.basename(tokens[0]) in ("python", "python3")

def detect_command(work_dir: str) -> str | None:
    if os.path.isfile(os.path.join(work_dir, "main.py")):
        return "python main.py"
    if os.path.isfile(os.path.join(work_dir, "app.py")):
        return "python app.py"
    if os.path.isfile(os.path.join(work_dir, "run.py")):
        return "python run.py"
    pkg_path = os.path.join(work_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
            if pkg.get("scripts", {}).get("start"):
                return "npm start"
        except Exception:
            pass
    if os.path.isfile(os.path.join(work_dir, "Cargo.toml")):
        return "cargo run"
    if os.path.isfile(os.path.join(work_dir, "go.mod")):
        return "go run ."
    return None


def detect_test_command(work_dir: str) -> str | None:
    has_test_files = any(
        f.startswith("test_") or f.endswith("_test.py")
        for _, _, files in os.walk(work_dir)
        for f in files
    )
    has_pytest_cfg = any(
        os.path.isfile(os.path.join(work_dir, f))
        for f in ("pytest.ini", "setup.cfg", "pyproject.toml", "tox.ini")
    )
    if has_test_files or has_pytest_cfg:
        return "pytest"
    pkg_path = os.path.join(work_dir, "package.json")
    if os.path.isfile(pkg_path):
        try:
            with open(pkg_path, encoding="utf-8") as f:
                pkg = json.load(f)
            if pkg.get("scripts", {}).get("test"):
                return "npm test"
        except Exception:
            pass
    if os.path.isfile(os.path.join(work_dir, "Cargo.toml")):
        return "cargo test"
    if os.path.isfile(os.path.join(work_dir, "go.mod")):
        return "go test ./..."
    return None


def infer_purpose(work_dir: str) -> str:
    for readme in ("README.md", "readme.md", "README.txt", "README.rst"):
        path = os.path.join(work_dir, readme)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read(1000).strip()
            except OSError:
                pass
    for entry in ("main.py", "app.py", "run.py", "index.js", "main.go", "main.rs"):
        path = os.path.join(work_dir, entry)
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f"Entry point ({entry}):\n" + f.read(500).strip()
            except OSError:
                pass
    return "A software project."

def prepare(fixture_path: str, command: str) -> str:
    dest = tempfile.mkdtemp(prefix="repo_fixer_")
    try:
        shutil.copytree(fixture_path, dest, dirs_exist_ok=True)
        if _is_python(command):
            subprocess.run(
                [sys.executable, "-m", "venv", ".venv"],
                cwd=dest, check=True, capture_output=True, text=True,
            )
        return dest
    except Exception:
        shutil.rmtree(dest, ignore_errors=True)
        raise

def install(work_dir: str, command: str, install_cmd: str | None = None) -> dict:
    if install_cmd:
        cmd_tokens = shlex.split(install_cmd)
        if not cmd_tokens:
            return {"ok": False, "output": "Empty install command"}
        if _is_python(command) and cmd_tokens[0] in ("pip", "pip3"):
            cmd_tokens = [_venv_python(work_dir), "-m", "pip"] + cmd_tokens[1:]
        result = subprocess.run(cmd_tokens, cwd=work_dir, capture_output=True, text=True)
        return {"ok": result.returncode == 0, "output": _truncate(result.stdout + result.stderr)}
    if _is_python(command):
        req_path = os.path.join(work_dir, "requirements.txt")
        if not os.path.isfile(req_path):
            return {"ok": True, "output": ""}
        result = subprocess.run(
            [_venv_python(work_dir), "-m", "pip", "install", "-r", "requirements.txt"],
            cwd=work_dir, capture_output=True, text=True,
        )
        return {"ok": result.returncode == 0, "output": _truncate(result.stdout + result.stderr)}
    return {"ok": True, "output": ""}

def run_target(work_dir: str, command: str, timeout: int = 60) -> dict:
    tokens = shlex.split(command)
    if _is_python(command):
        tokens[0] = _venv_python(work_dir)
    try:
        result = subprocess.run(
            tokens, cwd=work_dir, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Timed out", "returncode": -1}
    combined = result.stdout + result.stderr
    return {"ok": result.returncode == 0, "output": _truncate(combined), "returncode": result.returncode}

def run_install_cmd(work_dir: str, cmd: str, command: str) -> dict:
    try:
        tokens = shlex.split(cmd)
    except ValueError as e:
        return {"ok": False, "output": f"Could not parse command: {e}"}
    if not tokens:
        return {"ok": False, "output": "Empty command"}
    if _is_python(command) and tokens[0] in ("pip", "pip3"):
        tokens = [_venv_python(work_dir), "-m", "pip"] + tokens[1:]
    elif _is_python(command) and os.path.basename(tokens[0]) in ("python", "python3"):
        tokens[0] = _venv_python(work_dir)
    result = subprocess.run(tokens, cwd=work_dir, capture_output=True, text=True)
    return {"ok": result.returncode == 0, "output": _truncate(result.stdout + result.stderr)}

def run_tests(work_dir: str, test_cmd: str, command: str, timeout: int = 120) -> dict:
    try:
        tokens = shlex.split(test_cmd)
    except ValueError as e:
        return {"ok": False, "output": f"Could not parse test command: {e}"}
    if not tokens:
        return {"ok": False, "output": "Empty test command"}
    if tokens[0] == "pytest" and _is_python(command):
        tokens = [_venv_python(work_dir), "-m", "pytest"] + tokens[1:]
    try:
        result = subprocess.run(
            tokens, cwd=work_dir, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "output": "Tests timed out"}
    return {"ok": result.returncode == 0, "output": _truncate(result.stdout + result.stderr)}


def apply_edit(work_dir: str, filename: str, content: str) -> None:
    real_work_dir = os.path.realpath(work_dir)
    target = os.path.realpath(os.path.join(work_dir, filename))

    if not (target == real_work_dir or target.startswith(real_work_dir + os.sep)):
        raise ValueError(
            f"Path '{filename}' resolves to '{target}' which escapes the "f"work directory '{real_work_dir}'."
        )
    parent = os.path.dirname(target)
    os.makedirs(parent, exist_ok=True)
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(content)
