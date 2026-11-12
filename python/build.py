#!/usr/bin/env python3
"""Build the Louisenlund GUI as a native desktop binary with PyInstaller.

Run this script on the OS you want to build for. PyInstaller does not provide
reliable cross-compilation between Windows, macOS and Linux.

Examples:
    python -m pip install -r requirements-build.txt
    python build.py
    python build.py --skip-archive
"""

from __future__ import annotations

import argparse
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
# Prefer the filename used by the current repository; retain fallbacks for
# older project layouts.
APP_SOURCE_CANDIDATES = (
    ROOT / "schedule_gui.py",
)
APP_SOURCE = next((path for path in APP_SOURCE_CANDIDATES if path.is_file()), None)
APP_NAME = "LouisenlundSchedule"


def read_os_release() -> dict[str, str]:
    """Read Linux distribution metadata without adding a dependency."""
    result: dict[str, str] = {}
    path = Path("/etc/os-release")
    if not path.exists():
        return result
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if "=" not in line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        result[key] = value.strip().strip('"').lower()
    return result


def platform_tag() -> tuple[str, str]:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "amd64": "x86_64",
        "x64": "x86_64",
        "x86_64": "x86_64",
        "arm64": "arm64",
        "aarch64": "aarch64",
    }.get(machine, machine.replace(" ", "_"))

    if system == "windows":
        return "windows", f"windows-{architecture}"
    if system == "darwin":
        return "macos", f"macos-{architecture}"
    if system == "linux":
        release = read_os_release()
        distro_id = release.get("ID", "linux")
        id_like = release.get("ID_LIKE", "")
        arch_ids = {"arch", "manjaro", "endeavouros", "garuda", "cachyos", "artix"}
        is_arch_family = distro_id in arch_ids or "arch" in id_like.split()
        distro_tag = "arch" if is_arch_family else distro_id
        if not is_arch_family:
            print(
                "WARNING: This is not a detected Arch-family distribution. "
                "For the requested Arch Linux build, run this script on Arch "
                "or use the included GitHub Actions workflow."
            )
        return "linux", f"linux-{distro_tag}-{architecture}"
    raise SystemExit(
        f"Unsupported build OS: {platform.system()}. "
        "Build on Windows, macOS, or Linux."
    )


def add_tree_to_zip(source: Path, archive: Path) -> None:
    """Zip a folder while retaining executable permission bits where possible."""
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for item in source.rglob("*"):
            if item.is_file():
                zf.write(item, Path(source.name) / item.relative_to(source))


def create_archive(os_kind: str, bundle: Path, archive: Path) -> None:
    archive.parent.mkdir(parents=True, exist_ok=True)
    if os_kind == "linux":
        # tar preserves executable permissions and symlinks used by PyInstaller.
        with tarfile.open(archive, "w:gz", dereference=False) as tf:
            tf.add(bundle, arcname=bundle.name, recursive=True)
    elif os_kind == "macos":
        # ditto is Apple's tool for preserving app-bundle metadata/resource forks.
        ditto = shutil.which("ditto")
        if ditto:
            subprocess.run(
                [ditto, "-c", "-k", "--sequesterRsrc", "--keepParent", str(bundle), str(archive)],
                check=True,
            )
        else:
            add_tree_to_zip(bundle, archive)
    else:
        add_tree_to_zip(bundle, archive)


def validate_linux_bundle(bundle: Path, app_name: str) -> None:
    """Fail early if the complete PyInstaller onedir runtime is not present."""
    executable = bundle / app_name
    internal = bundle / "_internal"
    if not executable.is_file():
        raise SystemExit(f"Linux launcher not found: {executable}")
    if not internal.is_dir():
        raise SystemExit(
            f"PyInstaller runtime folder is missing: {internal}\n"
            "Do not distribute only the executable; the whole application folder is required."
        )
    python_libraries = list(internal.glob("libpython*.so*"))
    if not python_libraries:
        raise SystemExit(
            f"No bundled libpython shared library was found in {internal}.\n"
            "The PyInstaller onedir build appears incomplete; remove build/ and artifacts/ "
            "and rebuild using the same Python environment."
        )

    # Provide a launcher that always runs from the application directory.
    launcher = bundle / "run.sh"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        'cd -- "$(dirname -- "${BASH_SOURCE[0]}")"\n'
        f'if [[ ! -d "_internal" || ! -f "_internal/{python_libraries[0].name}" ]]; then\n'
        '  echo "LouisenlundSchedule is incomplete: the _internal runtime folder is missing." >&2\n'
        '  echo "Extract the complete .tar.gz package; do not copy the executable on its own." >&2\n'
        '  exit 1\n'
        'fi\n'
        f'exec "./{app_name}" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(0o755)


def main() -> int:
    parser = argparse.ArgumentParser(description="Package Louisenlund Schedule for this OS.")
    parser.add_argument(
        "--skip-archive",
        action="store_true",
        help="Leave the PyInstaller output folder unarchived.",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Show a console window (useful for diagnosing startup errors).",
    )
    parser.add_argument(
        "--name",
        default=APP_NAME,
        help=f"Application name (default: {APP_NAME}).",
    )
    args = parser.parse_args()

    if APP_SOURCE is None:
        expected = ", ".join(path.name for path in APP_SOURCE_CANDIDATES)
        raise SystemExit(
            f"Application source not found in {ROOT}. Expected one of: {expected}"
        )
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        raise SystemExit(
            "PyInstaller is not installed. Run:\n"
            "  python -m pip install -r requirements-build.txt"
        )

    os_kind, tag = platform_tag()
    output_root = ROOT / "artifacts"
    dist_dir = output_root / tag
    work_dir = ROOT / "build" / tag
    spec_dir = ROOT / "build" / "spec"

    if dist_dir.exists():
        shutil.rmtree(dist_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    spec_dir.mkdir(parents=True, exist_ok=True)
    dist_dir.mkdir(parents=True, exist_ok=True)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onedir",
        "--name",
        args.name,
        "--distpath",
        str(dist_dir),
        "--workpath",
        str(work_dir),
        "--specpath",
        str(spec_dir),
        "--collect-all",
        "customtkinter",
    ]
    if not args.console:
        command.append("--windowed")
    command.append(str(APP_SOURCE))

    print(f"Building {args.name} for {tag}…")
    print("Command:", " ".join(f'"{part}"' if " " in part else part for part in command))
    subprocess.run(command, cwd=ROOT, check=True)

    bundle = dist_dir / f"{args.name}.app" if os_kind == "macos" else dist_dir / args.name
    if not bundle.exists():
        raise SystemExit(
            "PyInstaller finished, but the expected application bundle was not found: "
            f"{bundle}\nInspect the build output above."
        )

    if os_kind == "linux":
        validate_linux_bundle(bundle, args.name)

    if args.skip_archive:
        print(f"Build complete: {bundle}")
        return 0

    suffix = ".tar.gz" if os_kind == "linux" else ".zip"
    archive = output_root / f"{args.name}-{tag}{suffix}"
    if archive.exists():
        archive.unlink()
    create_archive(os_kind, bundle, archive)

    print("\nBuild complete.")
    print(f"Application: {bundle}")
    print(f"Package:     {archive}")
    if os_kind == "linux":
        print("Run on Arch Linux after extracting the archive:")
        print(f"  cd {args.name} && ./run.sh")
        print("IMPORTANT: use the artifacts/ output, not the intermediate build/ directory.")
    if os_kind == "macos":
        print("Note: the macOS app is not code-signed or notarized by this script.")
    elif os_kind == "windows":
        print("Note: the Windows executable is not Authenticode-signed by this script.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except subprocess.CalledProcessError as exc:
        print(f"Build failed with exit code {exc.returncode}.", file=sys.stderr)
        raise SystemExit(exc.returncode)