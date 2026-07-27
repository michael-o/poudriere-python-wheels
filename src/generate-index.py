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

def extract_metadata(wheel_path: Path, verbose: int) -> dict:
    metadata = {}
    try:
        with zipfile.ZipFile(wheel_path, "r") as zf:
            for name in zf.namelist():
                # Only accept top-level dist-info METADATA
                if name.count("/") == 1 and name.endswith(".dist-info/METADATA"):
                    if verbose > 1:
                        print(f"Inspecting metadata in {wheel_path.name}")
                    with zf.open(name) as meta_file:
                        for line in meta_file:
                            line = line.decode("utf-8").strip()
                            if line.startswith("Requires-Python:"):
                                metadata["data-requires-python"] = line.split(":", 1)[1].strip()
                    break  # Stop after the correct METADATA
    except Exception as e:
        if verbose > 1:
            print(f"Failed to inspect {wheel_path.name}: {e}")
    return metadata

def generate_project_index(project_dir: Path, files: list[Path], inspect_metadata: bool, verbose: int) -> None:
    lines = ["<html><body>"]
    for f in sorted(files):
        digest = sha256sum(f)
        attrs = ""
        if inspect_metadata:
            meta = extract_metadata(f, verbose)
            for k, v in meta.items():
                attrs += f' {k}="{html.escape(v)}"'
        lines.append(
            f'<a href="{html.escape(f.name)}#sha256={digest}"{attrs}>{html.escape(f.name)}</a><br>'
        )
    lines.append("</body></html>")
    index_path = project_dir / "index.html"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    if verbose > 1:
        print(f"Generated {index_path}")

def generate_root_index(simple_dir: Path, projects: list[str], verbose: int) -> None:
    lines = ["<html><body>"]
    for p in sorted(projects):
        lines.append(f'<a href="{p}/">{p}</a><br>')
    lines.append("</body></html>")
    index_path = simple_dir / "index.html"
    index_path.write_text("\n".join(lines), encoding="utf-8")
    if verbose > 0:
        print(f"Generated {index_path}")

def build_index(wheel_dir: Path, inspect_metadata: bool, symlink: bool, verbose: int, force: bool) -> None:
    if verbose > 0:
        print(f"Generating static index in: {wheel_dir}")

    simple_dir = wheel_dir / "simple"
    simple_dir.mkdir(exist_ok=True)

    projects = {}
    changed_projects = set()
    new_project_seen = False
    for f in wheel_dir.iterdir():
        if f.suffix == ".whl":
            project_name = normalize_name(f.name.split("-")[0])
            proj_dir = simple_dir / project_name
            if not proj_dir.exists():
                proj_dir.mkdir()
                new_project_seen = True
            dest = proj_dir / f.name
            if not dest.exists():
                changed_projects.add(project_name)
                if symlink:
                    rel_target = os.path.relpath(f, proj_dir)
                    dest.symlink_to(rel_target)
                    if verbose > 1:
                        print(f"Symlinked {dest} → {rel_target}")
                else:
                    dest.write_bytes(f.read_bytes())
                    if verbose > 1:
                        print(f"Copied {f.name} → {dest}")
            projects.setdefault(project_name, []).append(dest)

    # Wheel filenames are immutable once published (build tags and
    # multiplatform windows only ever add new files), so a project's
    # index only needs regenerating when a file was actually added to
    # it this run, or it doesn't have one yet.
    for proj, files in projects.items():
        proj_dir = simple_dir / proj
        if force or proj in changed_projects or not (proj_dir / "index.html").exists():
            generate_project_index(proj_dir, files, inspect_metadata, verbose)
        elif verbose > 1:
            print(f"Unchanged, skipping: {proj_dir / 'index.html'}")

    root_index = simple_dir / "index.html"
    if force or new_project_seen or not root_index.exists():
        generate_root_index(simple_dir, list(projects.keys()), verbose)
    elif verbose > 1:
        print(f"Unchanged, skipping: {root_index}")

def main():
    parser = argparse.ArgumentParser(
        description="Generate a Simple Repository API index in WHEEL_DIR/simple/"
    )
    parser.add_argument(
        "wheel_dir",
        type=Path,
        metavar="WHEEL_DIR",
        help="Directory containing wheels"
    )
    parser.add_argument(
        "--inspect-metadata",
        action="store_true",
        help="Open wheels to extract metadata"
    )
    parser.add_argument(
        "-v", "--verbose",
        action="count",
        default=0,
        help="Enable verbose output; repeat (-vv) for per-file output"
    )
    parser.add_argument(
        "-S", "--no-symlink",
        action="store_true",
        help="Copy files into WHEEL_DIR/simple/ instead of symlinking (default: use relative symlinks)"
    )
    parser.add_argument(
        "-f", "--force",
        action="store_true",
        help="Regenerate every index, even for projects with no new wheels"
    )
    args = parser.parse_args()

    symlink = True
    if args.no_symlink:
        symlink = False

    build_index(args.wheel_dir, args.inspect_metadata, symlink, args.verbose, args.force)

if __name__ == "__main__":
    main()
