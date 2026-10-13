"""Allowlisted Lambda build. SAM invokes this inside its Linux build container."""
import argparse
import pathlib
import shutil
import subprocess
import sys

SOURCE = pathlib.Path(__file__).resolve().parents[1]
PACKAGES = ("utils", "scanner", "cost_engine", "compliance", "reporting", "delivery", "orchestration")


def build(target, dependencies=True):
    target = pathlib.Path(target).resolve()
    if target == SOURCE or target in SOURCE.parents:
        raise ValueError("Build target must not be the source or its parent")
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError("Build target must be empty")
    if dependencies:
        if sys.platform != "linux" or sys.version_info[:2] != (3, 12):
            raise RuntimeError("Dependency builds require the Python 3.12 Linux SAM container")
        subprocess.run([sys.executable, "-m", "pip", "install", "--only-binary=:all:",
                        "-r", str(SOURCE/"requirements.lock"), "--target", str(target)], check=True)
    shutil.copy2(SOURCE/"lambda_handler.py", target/"lambda_handler.py")
    for name in PACKAGES:
        for source in (SOURCE/name).rglob("*.py"):
            if source.is_symlink() or "__pycache__" in source.parts:
                continue
            destination = target/source.relative_to(SOURCE)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
    forbidden = {".env", ".git", ".aws", "tests", "htmlcov", "pytest", "pytest_cov"}
    if any(p.name in forbidden for p in target.rglob("*")):
        raise RuntimeError("Unexpected development or sensitive content in artifact")
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("target")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    build(args.target, not args.source_only)
