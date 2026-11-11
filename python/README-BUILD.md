# Louisenlund Schedule — binary packaging

This project uses **PyInstaller** to package the CustomTkinter app as a native desktop application.

## Important: build on each target OS

PyInstaller is not a cross-compiler. Build each binary on the operating system it is meant to run on:

- **Windows:** run `build.py` on Windows.
- **macOS:** run `build.py` on macOS. The result is an `.app` bundle.
- **Arch Linux / Arch derivatives:** run `build.py` on Arch, Manjaro, EndeavourOS, Garuda, CachyOS, or another Arch-family distribution.

The included GitHub Actions workflow (`.github/workflows/build-binaries.yml`) builds all three platforms automatically when manually dispatched or when a `v*` tag is pushed. Each build is published as a downloadable workflow artifact. The Linux job builds inside the official `archlinux:latest` container.

## Local build

Install Python 3 and the Tk development/runtime package first. On Arch Linux:

```bash
sudo pacman -Syu --needed python python-pip tk base-devel
```

In this project directory, create an environment and install the build requirements:

```bash
python -m venv .venv
```

Activate it, then run:

**Windows PowerShell**

```powershell
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-build.txt
python build.py
```

**macOS / Linux**

```bash
source .venv/bin/activate
python -m pip install -r requirements-build.txt
python build.py
```

The build output is written to `artifacts/`:

- Windows: `LouisenlundSchedule-windows-<architecture>.zip`
- macOS: `LouisenlundSchedule-macos-<architecture>.zip`
- Arch Linux: `LouisenlundSchedule-linux-arch-<architecture>.tar.gz`

The unpacked PyInstaller application folder or `.app` bundle is kept alongside the archive. Use `python build.py --console` when diagnosing startup errors. Use `python build.py --skip-archive` to keep only the unpacked output.

## Running the packaged app

- **Windows:** extract the ZIP and launch `LouisenlundSchedule.exe` inside the `LouisenlundSchedule` folder.
- **macOS:** extract the ZIP and open `LouisenlundSchedule.app`.
- **Arch Linux:** extract the TAR.GZ and run `./LouisenlundSchedule/LouisenlundSchedule` from a terminal or desktop launcher.

Tkinter requires a working desktop session. On Linux, this normally means an X11 or Wayland session with the appropriate XWayland/Tk support installed.

## Platform notes

- Builds are native to the machine architecture that performs them (for example, x86_64 vs. ARM64). Run a separate build on each architecture you intend to distribute.
- The macOS app is not code-signed or notarized. macOS may show a Gatekeeper warning for downloaded builds. A Developer ID certificate and notarization process are separate steps.
- The Windows executable is not Authenticode-signed. Windows SmartScreen may show an unknown-publisher warning.
- The script uses PyInstaller's `--onedir` mode for more dependable Tk/CustomTkinter packaging. The package is a folder containing the executable and its runtime files, distributed as a ZIP/TAR.GZ archive.
- The application currently disables TLS certificate verification for its Louisenlund API requests, matching the existing working configuration. Packaging does not change that behavior; it weakens protection against intercepted connections.
- Schedule JSON files are saved in the app's working directory under `data/` and raw API responses under `temp/`. Choose a writable working directory when launching the app.
