#!/usr/bin/env python3
"""One-shot setup for a new Windows PC. Safe to re-run.

    python setup_env.py            # install what is missing, fetch models, verify
    python setup_env.py --no-pip   # skip pip, just models + verify

Does four things, each idempotent:
  1. checks the Python version
  2. pip-installs the libraries (skipping any that are already there)
  3. downloads the model files
  4. runs doctor.py and reports whether the machine is ready

ffmpeg is NOT installed by this script on purpose: it is a large binary that
users usually already have, and silently downloading one from a third party is
not something a setup script should do behind your back. If it is missing,
doctor.py says so and prints where to get it.
"""
import argparse
import importlib.util
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))

# pip name -> import name, for packages whose import differs from the pip name.
# Anything not listed is assumed to import under its own name.
IMPORT_NAMES = {
    "opencv-python": "cv2",
    "pillow": "PIL",
    "faster-whisper": "faster_whisper",
    "sherpa-onnx": "sherpa_onnx",
    "scikit-image": "skimage",
    "python-dateutil": "dateutil",
}


def read_requirements(path=None):
    """pip names from requirements.txt.

    Read from the file rather than kept as a second hardcoded list: two copies
    of the same list drift, and the drift is invisible until someone installs
    on a fresh machine and a package is quietly missing.
    """
    path = path or os.path.join(_HERE, "requirements.txt")
    names = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    names.append(line)
    except OSError:
        pass
    return names


def import_name(pkg):
    base = pkg.split("==")[0].split(">=")[0].split("[")[0].strip().lower()
    return IMPORT_NAMES.get(base, base.replace("-", "_"))


def run(cmd, **kw):
    print(f"    $ {' '.join(cmd)}")
    return subprocess.run(cmd, **kw)


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--no-pip", action="store_true")
    ap.add_argument("--no-models", action="store_true")
    a = ap.parse_args()

    print("=" * 70)
    print("  Shortsmith setup")
    print("=" * 70)

    v = sys.version_info
    print(f"\n[1/4] Python {v.major}.{v.minor}.{v.micro}")
    print(f"      {sys.executable}")
    if v < (3, 9):
        print("\n  This needs Python 3.9 or newer.")
        print("  Get one from https://www.python.org/downloads/windows/")
        print("  and tick 'Add python.exe to PATH' during the install.")
        return 1

    # ---- libraries
    print("\n[2/4] libraries")
    if a.no_pip:
        print("      skipped (--no-pip)")
    else:
        pkgs = read_requirements()
        todo = [p for p in pkgs
                if importlib.util.find_spec(import_name(p)) is None]
        if not pkgs:
            print("      requirements.txt is missing or empty — nothing to install")
        elif not todo:
            print("      all already installed")
        else:
            print(f"      installing: {', '.join(todo)}")
            r = run([sys.executable, "-m", "pip", "install", *todo])
            if r.returncode != 0:
                print("\n  pip failed. Common causes:")
                print("    - no internet, or a proxy is blocking pypi.org")
                print("    - a very new Python with no wheels yet; try 3.10-3.12")
                return 1

    # ---- models
    print("\n[3/4] models")
    if a.no_models:
        print("      skipped (--no-models)")
    else:
        r = run([sys.executable, os.path.join(_HERE, "download_models.py")])
        if r.returncode != 0:
            print("\n  Some models could not be downloaded — check the connection")
            print("  and re-run; finished files are kept and skipped.")

    # ---- verify
    print("\n[4/4] verification")
    r = run([sys.executable, os.path.join(_HERE, "doctor.py")])
    if r.returncode == 0:
        print("\nSetup complete. Double-click Shortsmith_App.bat, or:")
        print('  python make_shorts.py "C:/path/to/video.mp4" --open')
    else:
        print("\nSetup finished but the machine is not ready yet —")
        print("fix the [FAIL] lines above and re-run: python setup_env.py")
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
