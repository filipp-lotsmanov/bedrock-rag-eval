"""Build build/<name>.zip for Lambda: the src/<name> package plus its locked dependency group.

Dependencies come from uv.lock (`uv export --only-group <name>`), with hashes, installed as
aarch64 manylinux wheels for the Python version in infra/lambda_python_version, the file
Terraform also reads for the runtime. The zip is
byte-reproducible: entries are sorted by POSIX path with fixed timestamps and permissions,
so Terraform's source_code_hash only changes when code or locked dependencies change.

Usage: uv run python scripts/build_lambda.py ingest
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLATFORM = "aarch64-manylinux2014"
FIXED_TIME = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644 << 16
NOT_SHIPPED = {".lock", "bin", "share"}  # uv lock file, console scripts, man pages


PYTHON_VERSION_FILE = ROOT / "infra" / "lambda_python_version"


def python_version() -> str:
    """The Lambda runtime's Python version, e.g. "3.12", shared with Terraform."""
    if not PYTHON_VERSION_FILE.is_file():
        sys.exit(f"missing {PYTHON_VERSION_FILE} (expected e.g. '3.12')")
    return PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()


def install_dependencies(group: str, stage: Path, build_dir: Path) -> None:
    requirements = build_dir / f"{group}-requirements.txt"
    subprocess.run(
        [
            "uv",
            "export",
            "--frozen",
            "--only-group",
            group,
            "--no-emit-project",
            "--quiet",
            "-o",
            str(requirements),
        ],
        check=True,
    )
    subprocess.run(
        [
            "uv", "pip", "install",
            "--target", str(stage),
            "--python-platform", PLATFORM,
            "--python-version", python_version(),
            "--only-binary", ":all:",
            "--requirement", str(requirements),
        ],
        check=True,
    )  # fmt: skip


def write_zip(stage: Path, out: Path) -> None:
    files = sorted(
        (
            path
            for path in stage.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.relative_to(stage).parts[0] not in NOT_SHIPPED
        ),
        key=lambda path: path.relative_to(stage).as_posix(),
    )
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in files:
            info = zipfile.ZipInfo(path.relative_to(stage).as_posix(), date_time=FIXED_TIME)
            info.external_attr = FILE_MODE
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, path.read_bytes())


def build(name: str) -> Path:
    package = ROOT / "src" / name
    if not (package / "__init__.py").is_file():
        sys.exit(f"{package} is not a package")
    build_dir = ROOT / "build"
    stage = build_dir / name
    out = build_dir / f"{name}.zip"

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    shutil.copytree(package, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    install_dependencies(name, stage, build_dir)
    write_zip(stage, out)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("name", help="package under src/ and dependency group of the same name")
    print(build(parser.parse_args().name))
