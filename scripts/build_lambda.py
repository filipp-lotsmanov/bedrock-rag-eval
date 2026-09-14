"""Package a Lambda handler directory into a deployable zip."""

import argparse
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def build(name: str, deps: list[str]) -> Path:
    src = ROOT / "src" / name
    common = ROOT / "src" / "common"
    stage = ROOT / "build" / name
    out = ROOT / "build" / f"{name}.zip"

    if not src.is_dir():
        sys.exit(f"no such handler directory: {src}")

    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    shutil.copytree(src, stage, dirs_exist_ok=True)
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
                "--only-binary",
                ":all:",
                *deps,
            ],
            check=True,
        )

    out.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(stage.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                zf.write(path, path.relative_to(stage))

    return out


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("name")
    parser.add_argument("--dep", action="append", default=[])
    args = parser.parse_args()
    print(build(args.name, args.dep))
