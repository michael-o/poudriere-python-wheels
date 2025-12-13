#!/usr/bin/env python3
import argparse
import hashlib
import re
import zipfile
import os
from pathlib import Path
import html

def normalize_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()

def sha256sum(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def extract_metadata(wheel_path: Path, verbose: bool) -> dict:
    metadata = {}
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            for name in zf.namelist():
                if name.endswith("METADATA") and ".dist-info/" in name:
                    if verbose:
                        print(f"Inspecting metadata in {wheel_path.name}")
                    with zf.open(name) as meta_file:
                        for line in meta_file:
                            line = line.decode("utf-8").strip()
                            if line.startswith("Requires-Python:"):
                                metadata["data-requires-python"] = line.split(":", 1)[1].strip()
    except Exception as e:
        if verbose:
            print(f"Failed to inspect {wheel_path.name}: {e}")
    return metadata

def generate_project_index(project_dir: Path, files: list[Path], inspect_metadata: bool, verbose: bool) -> None:
    lines = ["<html><body>"]
    for f in sorted(files):
        digest = sha256sum(f)
        attrs = ""
        if inspect_metadata and f.suffix == ".whl":
            meta = extract_metadata(f, verbose)
            for k, v in meta.items():
                attrs += f' {k}="{html.escape(v)}"'
        lines.append(
            f'<a href="{html.escape(f.name)}#sha256={digest}"{attrs}>{html.escape(f.name)}</a><br>'
        )
    lines.append("</body></html>")
    index_path = project_dir / "index.html"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    if verbose:
        print(f"Generated {index_path}")

def generate_root_index(simple_dir: Path, projects: list[str], verbose: bool) -> None:
    lines = ["<html><body>"]
    for p in sorted(projects):
        lines.append(f'<a href="{p}/">{p}</a><br>')
    lines.append("</body></html>")
    index_path = simple_dir / "index.html"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    if verbose:
        print(f"Generated {index_path}")

def build_index(wheel_dir: Path, inspect_metadata: bool, symlink: bool, verbose: bool) -> None:
    simple_dir = wheel_dir / "simple"
    simple_dir.mkdir(exist_ok=True)

    projects = {}
    for f in wheel_dir.iterdir():
        if f.suffix in [".whl", ".tar.gz", ".zip"]:
            project_name = normalize_name(f.name.split("-")[0])
            proj_dir = simple_dir / project_name
            proj_dir.mkdir(exist_ok=True)
            dest = proj_dir / f.name
            if not dest.exists():
                if symlink:
                    rel_target = os.path.relpath(f, proj_dir)
                    dest.symlink_to(rel_target)
                    if verbose:
                        print(f"Symlinked {dest} → {rel_target}")
                else:
                    dest.write_bytes(f.read_bytes())
                    if verbose:
                        print(f"Copied {f.name} → {dest}")
            projects.setdefault(project_name, []).append(dest)

    for proj, files in projects.items():
        generate_project_index(simple_dir / proj, files, inspect_metadata, verbose)

    generate_root_index(simple_dir, list(projects.keys()), verbose)

def main():
    parser = argparse.ArgumentParser(
        description="Generate a Simple Repository API index in WHEEL_DIR/simple/"
    )
    parser.add_argument(
        "wheel_dir",
        type=Path,
        metavar="WHEEL_DIR",
        help="Directory containing wheels/sdists"
    )
    parser.add_argument(
        "--inspect-metadata",
        action="store_true",
        help="Open wheels to extract metadata"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose output"
    )
    parser.add_argument(
        "-s", "--symlink",
        action="store_true",
        help="Use relative symlinks in WHEEL_DIR/simple/ (default)"
    )
    parser.add_argument(
        "-S", "--no-symlink",
        action="store_true",
        help="Copy files into WHEEL_DIR/simple/ instead of symlinking"
    )
    args = parser.parse_args()

    symlink = True
    if args.no_symlink:
        symlink = False

    build_index(args.wheel_dir, args.inspect_metadata, symlink, args.verbose)

if __name__ == "__main__":
    main()
