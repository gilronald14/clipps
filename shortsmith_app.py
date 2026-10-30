#!/usr/bin/env python3
"""Shortsmith — a small local window for turning one long video into shorts.

Pick a video, press Start, watch the log. Nothing is uploaded anywhere and no
account or API key is needed: highlight selection runs offline.

Launch it with shortsmith_app.bat (double-click), or:
    python shortsmith_app.py

Drag-and-drop straight onto the window is not wired up because Tk needs the
optional tkinterdnd2 extension for it — use Shortsmith_Drop.bat instead, which
takes a dropped file on its icon.
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

VIDEO_TYPES = [("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv"),
               ("All files", "*.*")]


def _num(value, cast, fallback):
    """A remembered number, or the fallback when it is missing or unusable."""
    try:
        return cast(value)
    except (TypeError, ValueError):
        return fallback


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

        ttk.Label(opts, text="How many shorts:").grid(row=0, column=0, padx=8, pady=8)
        ttk.Spinbox(opts, from_=1, to=30, width=4, textvariable=self.clips).grid(
            row=0, column=1, padx=(0, 18))
        ttk.Label(opts, text="Clip length (seconds):").grid(row=0, column=2)
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

        self.say("Pick a video, then press START.")
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
