#!/usr/bin/env python3
"""Put Shortsmith on the desktop and the Start menu.

A folder of scripts does not feel like an installed program; a shortcut that
opens the window does. This is deliberately the LAST, optional step of setup
and it fails soft: a locked-down machine that forbids shortcuts still gets a
working install, just without the icon.

The .lnk format is a COM object, so PowerShell's WScript.Shell writes it rather
than reimplementing the binary layout. The icon is drawn with Pillow, which the
installer already requires, so no binary asset has to be committed.

    python install_shortcuts.py             # create desktop + start menu entries
    python install_shortcuts.py --remove    # take them away again
"""
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
NAME = "Shortsmith"
TARGET = os.path.join(_HERE, "Shortsmith_App.bat")
ICON = os.path.join(_HERE, "shortsmith.ico")


def make_icon(path=ICON):
    """Draw the icon, if it is not already there.

    Returns the path on success, or None if Pillow is missing or writing
    failed — the shortcut is still worth making without a custom icon.
    """
    if os.path.exists(path):
        return path
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return None
    try:
        s = 256
        img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)

        # Rounded dark card
        d.rounded_rectangle([8, 8, s - 8, s - 8], radius=48, fill=(17, 20, 24, 255))

        # A 9:16 frame — the shape the tool actually makes
        fw, fh = 92, 164
        fx, fy = (s - fw) // 2, (s - fh) // 2
        d.rounded_rectangle([fx, fy, fx + fw, fy + fh], radius=16,
                            fill=(32, 38, 46, 255), outline=(94, 168, 255, 255), width=5)

        # Play triangle inside it
        cx, cy, r = s // 2, s // 2, 26
        d.polygon([(cx - r + 8, cy - r), (cx - r + 8, cy + r), (cx + r, cy)],
                  fill=(94, 168, 255, 255))

        # A caption bar under the frame, as burned captions produce
        d.rounded_rectangle([fx + 10, fy + fh - 34, fx + fw - 10, fy + fh - 14],
                            radius=6, fill=(94, 168, 255, 210))

        img.save(path, format="ICO",
                 sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
        return path
    except (OSError, ValueError):
        return None


def _powershell(script):
    """Run a PowerShell snippet. Returns (ok, message)."""
    for exe in ("powershell", "pwsh"):
        try:
            r = subprocess.run([exe, "-NoProfile", "-NonInteractive",
                                "-ExecutionPolicy", "Bypass", "-Command", script],
                               capture_output=True, text=True, timeout=60)
            return r.returncode == 0, (r.stdout + r.stderr).strip()
        except (OSError, subprocess.SubprocessError):
            continue
    return False, "PowerShell is not available"


def _destinations():
    """Folders to drop a shortcut into, skipping any that do not exist."""
    home = os.path.expanduser("~")
    desktop = os.path.join(home, "Desktop")
    start = os.path.join(os.environ.get("APPDATA", home), "Microsoft", "Windows",
                         "Start Menu", "Programs")
    return [d for d in (desktop, start) if os.path.isdir(d)]


def main():
    remove = "--remove" in sys.argv

    if not os.path.exists(TARGET):
        raise SystemExit(f"cannot find {TARGET}\n"
                         "run this from the folder you extracted the program into")

    icon = None if remove else make_icon()
    made, failed = [], []

    for folder in _destinations():
        lnk = os.path.join(folder, NAME + ".lnk")
        if remove:
            try:
                os.remove(lnk)
                made.append(lnk)
            except OSError:
                pass
            continue

        # Single-quote for PowerShell; a doubled quote is its escape.
        q_lnk = lnk.replace("'", "''")
        q_target = TARGET.replace("'", "''")
        q_here = _HERE.replace("'", "''")
        icon_line = (f"$s.IconLocation = '{icon.replace(chr(39), chr(39) * 2)},0'; "
                     if icon else "")
        script = (
            "$w = New-Object -ComObject WScript.Shell; "
            f"$s = $w.CreateShortcut('{q_lnk}'); "
            f"$s.TargetPath = '{q_target}'; "
            f"$s.WorkingDirectory = '{q_here}'; "
            f"$s.Description = 'Turn a long video into vertical shorts, locally'; "
            f"{icon_line}"
            "$s.Save()"
        )
        ok, msg = _powershell(script)
        if ok and os.path.exists(lnk):
            made.append(lnk)
        else:
            failed.append((lnk, msg))

    if remove:
        print(f"  removed {len(made)} shortcut(s)")
        return 0

    if made:
        print("  added a shortcut to:")
        for m in made:
            print(f"    {m}")
    if failed:
        print("  could not add:")
        for lnk, msg in failed:
            print(f"    {lnk}")
            if msg:
                print(f"      {msg.splitlines()[0][:100]}")
        print("  (not a problem — launch Shortsmith_App.bat directly)")

    if not made and not failed:
        print("  no Desktop or Start Menu folder found — skipping")

    if icon:
        print(f"  icon: {icon}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
