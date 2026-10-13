"""Inspect a build and smoke-test every deployed handler in a clean process."""
import pathlib
import json
import subprocess
import sys


def validate(target):
    target = pathlib.Path(target).resolve()
    if not (target / 'lambda_handler.py').is_file():
        raise ValueError('Artifact has no Lambda handler')
    for path in target.rglob("*"):
        if path.name in {".env", ".aws", ".git", "tests", "htmlcov", "pytest", "pytest_cov"}:
            raise ValueError(f"Forbidden artifact entry: {path.name}")
    lock = pathlib.Path(__file__).resolve().parents[1] / 'requirements.lock'
    expected = dict(line.split('==', 1) for line in lock.read_text(encoding='utf-8').splitlines()
                    if line and not line.startswith('#'))
    # -S prevents dependencies installed on the validation host masking an
    # incomplete artifact. Distribution metadata must also match the lock.
    subprocess.run([sys.executable, "-I", "-S", "-B", "-c",
        "import sys, json, importlib.metadata as metadata; sys.path.insert(0, sys.argv[1]); "
        "expected = json.loads(sys.argv[2]); "
        "assert all(metadata.version(name) == version for name, version in expected.items()), 'Dependency lock mismatch'; "
        "import lambda_handler; "
        "import orchestration.start_run, orchestration.scanner_handler, orchestration.finalize_run; "
        "from utils.config import Config; Config.load()", str(target), json.dumps(expected)], check=True, cwd=target)


if __name__ == "__main__":
    validate(sys.argv[1])
