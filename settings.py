#!/usr/bin/env python3
"""Where Shortsmith remembers your choices between runs.

Kept in the user's local app data rather than beside the code, for two
reasons: the program folder may be a read-only git checkout, and a chosen
output folder belongs to the person using it, not to the installation. Two
people sharing one install should still get their own folders.

Everything here fails soft. Remembering a folder is a convenience — if it
cannot be read or written the program carries on with defaults rather than
refusing to render over a settings file.
"""
import json
import os

APP = "Shortsmith"


def path():
    """Where the settings live."""
    base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return os.path.join(base, APP, "settings.json")


def load():
    """The saved settings as a dict; {} if there are none or they are unreadable."""
    try:
        with open(path(), encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save(**kw):
    """Merge kw into the saved settings.

    Merges rather than replaces so callers can each own one key without
    clobbering the others — the window remembers the video and output folder,
    make_shorts.py remembers where it last wrote.
    """
    data = load()
    data.update(kw)
    try:
        os.makedirs(os.path.dirname(path()), exist_ok=True)
        with open(path(), "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except OSError:
        pass


def get(key, default=None):
    return load().get(key, default)


def default_source():
    """Last video used, or empty. Validated by the caller before use."""
    p = get("source") or ""
    return p if p and os.path.exists(p) else ""


def default_out():
    """Where shorts go when nothing has been chosen yet."""
    return get("out") or os.path.join(os.path.expanduser("~"), "Videos", "shorts")


if __name__ == "__main__":
    import sys
    if "--path" in sys.argv:
        print(path())
    else:
        print(json.dumps(load(), indent=2))
