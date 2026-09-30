"""Package a Lambda handler directory into a deployable zip.

The handler directory is staged as a package (build/<name>/<name>/), so
imports resolve the same way in Lambda as in the test suite:
Terraform handler = "<name>.handler.main".

The zip is byte-for-byte reproducible: entries are sorted and written with
a fixed timestamp and fixed permissions, so an unchanged build produces an
unchanged source_code_hash and Terraform plans no redeploy. This holds across
operating systems only if sources have LF line endings (..gitattributes).
"""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION_FILE = ROOT / "infra" / "lambda_python_version"
"""Single source of truth for the runtime, read by Terraform as well."""

FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
FILE_MODE = 0o644 << 16


def lambda_python_version() -> str:
    if not PYTHON_VERSION_FILE.is_file():
        sys.exit(f"missing {PYTHON_VERSION_FILE} (expected e.g. '3.12')")
    return PYTHON_VERSION_FILE.read_text(encoding="utf-8").strip()


def write_reproducible_zip(stage: Path, out: Path) -> None:
    """Sort on the POSIX path string, not on Path objects.

    Windows paths compare case-insensitively, so sorting Path objects gives a
    different entry order (and zip hash) on Windows than on Linux CI.
    """
    entries = sorted(
        (path.relative_to(stage).as_posix(), path)
        for path in stage.rglob("*")
        if path.is_file() and "__pycache__" not in path.relative_to(stage).parts
    )
    with zipfile.ZipFile(out, "w") as zf:
        for arcname, path in entries:
            info = zipfile.ZipInfo(arcname, date_time=FIXED_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = FILE_MODE
            zf.writestr(info, path.read_bytes())


def build(name: str, deps: list[str]) -> Path:
    src = ROOT / "src" / name
    common = ROOT / "src" / "common"
    stage = ROOT / "build" / name
    out = ROOT / "build" / f"{name}.zip"

    if not src.is_dir():
        sys.exit(f"no such handler directory: {src}")

    python_version = lambda_python_version()

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    shutil.copytree(src, stage / name)
    if common.is_dir():
        shutil.copytree(common, stage / "common")

    if deps:
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--target",
                str(stage),
                "--python-platform",
                "aarch64-manylinux2014",
                "--.python-version",
                python_version,
                "--only-binary",
                ":all:",
                *deps,
            ],
            check=True,
        )

    out.parent.mkdir(exist_ok=True)
    write_reproducible_zip(stage, out)
    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--dep", action="append", default=[])
    args = parser.parse_args()
    print(build(args.name, args.dep))
