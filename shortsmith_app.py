#!/usr/bin/env python3
"""Shortsmith — a small local window for turning one long video into shorts.

Pick a video, press Start, watch the log. Nothing is uploaded anywhere and no
account or API key is needed: highlight selection runs offline.

Launch it with shortsmith_app.bat (double-click), or:
    python shortsmith_app.py

Three ways to give it a video, all equivalent: drag one onto this window,
drop it on Shortsmith_Drop.bat, or press Choose…

Drag-and-drop needs no extra package: Tk has no built-in file drop, so the
window hooks the Windows facility directly (shell32 DragAcceptFiles plus
WM_DROPFILES) instead of depending on the tkinterdnd2 Tcl extension. If that
hook cannot be installed the window still opens and works; it just says so.
"""
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import settings                                              # noqa: E402

VIDEO_EXTS = (".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".flv")
VIDEO_TYPES = [("Video files", " ".join("*" + e for e in VIDEO_EXTS)),
               ("All files", "*.*")]


def _num(value, cast, fallback):
    """A remembered number, or the fallback when it is missing or unusable."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return fallback


# ----------------------------------------------------------------------
# Drag and drop
#
# Tk has no built-in file drop on Windows, and the usual answer
# (tkinterdnd2) ships a Tcl extension binary — an extra dependency that has
# to work on whatever PC installs this. The OS facility needs none: tell
# shell32 the window accepts drops, then read the paths out of the
# WM_DROPFILES message it sends us.
#
# All of it is fail-soft. If any piece cannot be set up the window opens and
# works exactly as before, just without accepting drops.
# ----------------------------------------------------------------------
WM_DROPFILES = 0x0233
WM_DESTROY = 0x0002
GWLP_WNDPROC = -4

try:
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    _LRESULT = ctypes.c_ssize_t
    _WNDPROC = ctypes.WINFUNCTYPE(_LRESULT, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
    # The ...Ptr names only exist in the 64-bit libraries; the 32-bit ones use
    # the plain name. Take whichever this interpreter actually has.
    _get_wndproc = getattr(_user32, "GetWindowLongPtrW", None) or _user32.GetWindowLongW
    _set_wndproc = getattr(_user32, "SetWindowLongPtrW", None) or _user32.SetWindowLongW
    _get_wndproc.restype = _LRESULT
    _get_wndproc.argtypes = [wintypes.HWND, ctypes.c_int]
    _set_wndproc.restype = _LRESULT
    _set_wndproc.argtypes = [wintypes.HWND, ctypes.c_int, _WNDPROC]
    _user32.CallWindowProcW.restype = _LRESULT
    _user32.CallWindowProcW.argtypes = [_LRESULT, wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
    _shell32.DragAcceptFiles.argtypes = [wintypes.HWND, wintypes.BOOL]
    _shell32.DragQueryFileW.restype = wintypes.UINT
    _shell32.DragQueryFileW.argtypes = [wintypes.HANDLE, wintypes.UINT,
                                        wintypes.LPWSTR, wintypes.UINT]
    _shell32.DragFinish.argtypes = [wintypes.HANDLE]
    _DND_READY = True
except Exception:                                    # noqa: BLE001
    _DND_READY = False


def _dropped_paths(hdrop):
    """The file paths inside a WM_DROPFILES handle."""
    paths = []
    try:
        # Passing 0xFFFFFFFF as the index asks for the count.
        count = _shell32.DragQueryFileW(hdrop, 0xFFFFFFFF, None, 0)
        for i in range(count):
            need = _shell32.DragQueryFileW(hdrop, i, None, 0)
            buf = ctypes.create_unicode_buffer(need + 1)
            _shell32.DragQueryFileW(hdrop, i, buf, need + 1)
            if buf.value:
                paths.append(buf.value)
    except Exception:                                # noqa: BLE001
        pass
    return paths


def enable_file_drop(root, on_drop):
    """Let files be dragged onto `root`; call on_drop([paths]).

    True when the OS hook is in place, False when the feature is simply
    absent — which is never a reason to fail the launch.
    """
    if not _DND_READY:
        return False
    try:
        # winfo_id() is Tk's own child window; Windows delivers drops to its
        # parent wrapper, so step up one level.
        hwnd = root.winfo_id()
        parent = _user32.GetParent(hwnd)
        if parent:
            hwnd = parent

        # Read the old procedure BEFORE installing ours, so the handler never
        # has to chain through a value that is still unset.
        previous = _get_wndproc(hwnd, GWLP_WNDPROC)
        if not previous:
            return False

        def handler(h, msg, wparam, lparam):
            if msg == WM_DROPFILES:
                paths = _dropped_paths(wparam)
                _shell32.DragFinish(wparam)
                if paths:
                    # Deferred: this runs inside the message pump, so let Tk
                    # get back to its own loop before we touch any widget.
                    root.after(0, lambda p=paths: on_drop(p))
                return 0
            if msg == WM_DESTROY:
                # Hand the window back before Tk tears it down. Windows holds a
                # raw pointer to our callback, so leaving it installed would
                # mean calling into a Python object that is being collected.
                try:
                    _set_wndproc(h, GWLP_WNDPROC, previous)
                except Exception:                    # noqa: BLE001
                    pass
                return _user32.CallWindowProcW(previous, h, msg, wparam, lparam)
            return _user32.CallWindowProcW(previous, h, msg, wparam, lparam)

        # Keep the callback referenced: Windows holds a raw pointer to it, so
        # collecting it would mean calling freed memory on the next message.
        proc = _WNDPROC(handler)
        _set_wndproc(hwnd, GWLP_WNDPROC, proc)
        root._drop_proc = proc
        root._drop_previous = previous
        root._drop_hwnd = hwnd

        _shell32.DragAcceptFiles(hwnd, True)
        return True
    except Exception:                                # noqa: BLE001
        return False


class App:
    def __init__(self, root):
        self.root = root
        self.proc = None
        self.q = queue.Queue()
        # Open on last time's choices rather than on defaults you would have to
        # change every run. Every value is re-validated: a settings file that
        # has been hand-edited or half-written must not take the window down.
        saved = settings.load()
        self.src = tk.StringVar(value=settings.default_source())
        self.out = tk.StringVar(value=settings.default_out())
        self.clips = tk.IntVar(value=_num(saved.get("clips"), int, 7))
        self.min_sec = tk.DoubleVar(value=_num(saved.get("min_sec"), float, 20.0))
        self.max_sec = tk.DoubleVar(value=_num(saved.get("max_sec"), float, 45.0))
        self.whisper = tk.StringVar(value=saved.get("whisper") or "base")
        self.use_llm = tk.BooleanVar(value=bool(saved.get("use_llm")))
        self.status = tk.StringVar(value="Ready.")

        root.title("Shortsmith — long video to vertical shorts")
        root.geometry("860x600")
        root.minsize(760, 520)

        pad = dict(padx=10, pady=6)
        row = 0

        head = ttk.Label(root, text="Shortsmith", font=("Segoe UI", 15, "bold"))
        head.grid(row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(12, 0))
        row += 1
        ttk.Label(root, text="Turns one long video into 1080x1920 shorts with "
                             "face-aware framing, motion and captions. "
                             "Runs entirely on this PC.").grid(
            row=row, column=0, columnspan=4, sticky="w", padx=10, pady=(0, 10))
        row += 1

        ttk.Label(root, text="Import video:").grid(row=row, column=0, sticky="e", **pad)
        ttk.Entry(root, textvariable=self.src).grid(row=row, column=1, columnspan=2,
                                                    sticky="ew", **pad)
        ttk.Button(root, text="Choose…", command=self.pick_video).grid(
            row=row, column=3, sticky="ew", **pad)
        row += 1

        ttk.Label(root, text="Export shorts to:").grid(row=row, column=0, sticky="e", **pad)
        ttk.Entry(root, textvariable=self.out).grid(row=row, column=1, columnspan=2,
                                                    sticky="ew", **pad)
        ttk.Button(root, text="Change…", command=self.pick_out).grid(
            row=row, column=3, sticky="ew", **pad)
        row += 1
        ttk.Label(root, text="Each video gets its own subfolder here, so clips "
                             "from different videos never mix.",
                  foreground="#6b7280").grid(row=row, column=1, columnspan=3,
                                             sticky="w", padx=10)
        row += 1

        opts = ttk.LabelFrame(root, text=" Options ")
        opts.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=8)
        row += 1

        ttk.Label(opts, text="How many clips:").grid(row=0, column=0, padx=8, pady=8)
        ttk.Spinbox(opts, from_=1, to=30, width=4, textvariable=self.clips).grid(
            row=0, column=1, padx=(0, 18))
        ttk.Label(opts, text="Length of each clip (seconds):").grid(row=0, column=2)
        ttk.Spinbox(opts, from_=8, to=120, width=4, textvariable=self.min_sec).grid(
            row=0, column=3, padx=(0, 4))
        ttk.Label(opts, text="to").grid(row=0, column=4)
        ttk.Spinbox(opts, from_=10, to=180, width=4, textvariable=self.max_sec).grid(
            row=0, column=5, padx=(4, 18))

        ttk.Label(opts, text="Speech accuracy:").grid(row=1, column=0, padx=8, pady=(0, 10))
        cb = ttk.Combobox(opts, textvariable=self.whisper, width=8, state="readonly",
                          values=("base", "small", "medium"))
        cb.grid(row=1, column=1, padx=(0, 18), pady=(0, 10))
        ttk.Label(opts, text="base is fastest; small/medium mis-hear less but "
                             "take longer.").grid(row=1, column=2, columnspan=4,
                                                  sticky="w", pady=(0, 10))
        ttk.Checkbutton(opts, text="Use an LLM to rank moments (needs an API key; "
                                   "offline ranking is used otherwise)",
                        variable=self.use_llm).grid(row=2, column=0, columnspan=6,
                                                    sticky="w", padx=8, pady=(0, 10))

        bar = ttk.Frame(root)
        bar.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10)
        row += 1
        self.start_btn = ttk.Button(bar, text="START", command=self.start)
        self.start_btn.pack(side="left")
        self.open_btn = ttk.Button(bar, text="Open output folder",
                                   command=self.open_out, state="disabled")
        self.open_btn.pack(side="left", padx=8)
        ttk.Button(bar, text="Quit", command=self.root.destroy).pack(side="right")

        self.pb = ttk.Progressbar(root, mode="indeterminate")
        self.pb.grid(row=row, column=0, columnspan=4, sticky="ew", padx=10, pady=(8, 0))
        row += 1
        ttk.Label(root, textvariable=self.status).grid(row=row, column=0, columnspan=4,
                                                       sticky="w", padx=10, pady=(4, 0))
        row += 1

        self.log = tk.Text(root, wrap="word", height=16, background="#111418",
                           foreground="#d6dde6", insertbackground="#d6dde6",
                           font=("Consolas", 9))
        self.log.grid(row=row, column=0, columnspan=4, sticky="nsew", padx=10, pady=10)
        sb = ttk.Scrollbar(root, command=self.log.yview)
        sb.grid(row=row, column=4, sticky="ns", pady=10)
        self.log.configure(yscrollcommand=sb.set)

        root.columnconfigure(1, weight=1)
        root.columnconfigure(2, weight=1)
        root.rowconfigure(row, weight=1)

        # Accept a dragged file as well as a chosen one. If the OS hook cannot
        # be installed the window still works; it just says so, once.
        self.drop_ok = enable_file_drop(root, self.on_drop_files)
        if self.drop_ok:
            self.say("Drag a video onto this window, or press Choose… — then START.")
        else:
            self.say("Press Choose… to pick a video, then START.")
            self.say("  (dragging onto the window is unavailable here; "
                     "Shortsmith_Drop.bat still takes a dropped file)")
        self.say("Nothing is uploaded — transcription, framing and rendering all "
                 "run locally with ffmpeg and Whisper.")
        self.root.after(120, self.drain)

    # ---------------- ui helpers ----------------
    def say(self, line):
        self.log.insert("end", line + "\n")
        self.log.see("end")

    def _start_dir(self, key):
        """A folder to open a browser at: last used, else beside the current file.

        Returning None is fine — tkinter drops an initialdir of None, so the
        dialog just falls back to its own default.
        """
        d = settings.get(key)
        if d and os.path.isdir(d):
            return d
        cur = os.path.dirname(self.src.get().strip())
        return cur if cur and os.path.isdir(cur) else None

    def on_drop_files(self, paths):
        """A file (or several) was dragged onto the window.

        A drop usually happens before anything is chosen, so this fills the
        import field the same way Choose… does — and remembers it, so a
        second drop of the same video later needs no thought.
        """
        if not paths:
            return
        videos = [p for p in paths
                  if os.path.splitext(p)[1].lower() in VIDEO_EXTS]
        if not videos:
            self.say("Dropped, but none of those look like a video:")
            for p in paths[:4]:
                self.say(f"  {os.path.basename(p)}")
            self.status.set("That wasn't a video file.")
            return
        chosen = videos[0]
        self.src.set(chosen)
        settings.save(source=chosen, last_video_dir=os.path.dirname(chosen))
        self.say(f"Dropped: {chosen}")
        if len(videos) > 1:
            self.say(f"  ({len(videos)} videos dropped — using the first)")
        self.status.set("Ready: " + os.path.basename(chosen)
                        + " — press START when you are.")

    def pick_video(self):
        p = filedialog.askopenfilename(title="Choose a video to import",
                                       filetypes=VIDEO_TYPES,
                                       initialdir=self._start_dir("last_video_dir"))
        if p:
            self.src.set(p)
            settings.save(source=p, last_video_dir=os.path.dirname(p))
            self.status.set("Selected: " + os.path.basename(p))

    def pick_out(self):
        p = filedialog.askdirectory(title="Where should the exported shorts go?",
                                    initialdir=self.out.get().strip() or None)
        if p:
            self.out.set(p)
            settings.save(out=p)
            self.status.set("Shorts will be exported to " + p)

    def open_out(self):
        p = self.out.get()
        try:
            os.startfile(p)                       # Windows
        except AttributeError:
            subprocess.run(["xdg-open", p], check=False)
        except OSError as e:
            messagebox.showwarning("Could not open", str(e))

    # ---------------- run ----------------
    def start(self):
        src = self.src.get().strip()
        if not src or not os.path.exists(src):
            messagebox.showerror("No video", "Choose a video file first.")
            return
        if self.proc and self.proc.poll() is None:
            messagebox.showinfo("Already running", "A job is already in progress.")
            return
        if float(self.min_sec.get()) >= float(self.max_sec.get()):
            messagebox.showerror("Bad length", "Minimum length must be less than maximum.")
            return

        out = self.out.get().strip() or settings.default_out()
        self.out.set(out)
        # Remember every choice so the next run opens exactly like this one --
        # and so drag-and-drop, which cannot ask, exports to the same place.
        settings.save(source=src, out=out,
                      clips=int(self.clips.get()),
                      min_sec=float(self.min_sec.get()),
                      max_sec=float(self.max_sec.get()),
                      whisper=self.whisper.get(),
                      use_llm=bool(self.use_llm.get()))

        cmd = [sys.executable, os.path.join(_HERE, "make_shorts.py"), src,
               "--clips", str(int(self.clips.get())),
               "--min", str(float(self.min_sec.get())),
               "--max", str(float(self.max_sec.get())),
               "--whisper", self.whisper.get(),
               "--out", out,
               "--work", os.path.join(_HERE, "work")]
        if self.use_llm.get():
            cmd.append("--llm")

        self.log.delete("1.0", "end")
        self.say(" ".join(f'"{c}"' if " " in c else c for c in cmd))
        self.say("")
        self.start_btn.configure(state="disabled")
        self.open_btn.configure(state="disabled")
        self.pb.start(12)
        self.status.set("Working — the log below updates as it goes.")

        self.proc = subprocess.Popen(cmd, cwd=_HERE, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     encoding="utf-8", errors="replace",
                                     bufsize=1)
        threading.Thread(target=self._pump, args=(self.proc,), daemon=True).start()

    def _pump(self, proc):
        for line in proc.stdout:
            self.q.put(line.rstrip("\n"))
        self.q.put(None)

    def drain(self):
        """Move whatever the child has printed into the text box."""
        done = False
        while True:
            try:
                line = self.q.get_nowait()
            except queue.Empty:
                break
            if line is None:
                done = True
                continue
            self.say(line)
            low = line.strip()
            if low.startswith("[1/3]"):
                self.status.set("Transcribing the audio…")
            elif low.startswith("[2/3]"):
                self.status.set("Choosing the highlight moments…")
            elif low.startswith("[3/3]"):
                self.status.set("Rendering the shorts — this is the slow part.")
            elif low.startswith("  DONE"):
                self.status.set(low)

        if done:
            rc = self.proc.returncode if self.proc else -1
            self.pb.stop()
            self.start_btn.configure(state="normal")
            self.open_btn.configure(state="normal")
            self.say("")
            self.say("Finished." if rc == 0 else f"Stopped with code {rc}.")
            if rc == 0:
                self.status.set("Done — press 'Open output folder'.")
            else:
                self.status.set("That did not finish cleanly; see the log above.")
        self.root.after(120, self.drain)


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista")
    except tk.TclError:
        pass
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
