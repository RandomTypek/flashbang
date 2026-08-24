#!/usr/bin/env python3
"""
Flashbang GUI - a small tkinter front-end for flashbang.py.

Drop this next to flashbang.py and run it:

    python3 flashbang_gui.py
    python3 flashbang_gui.py game.swf      # pre-loads a file

Stdlib only. On Arch you may need `pacman -S tk` for tkinter itself.
"""

import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import traceback

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

FROZEN = getattr(sys, "frozen", False)
if FROZEN:
    # PyInstaller: modules live in the bundle, the exe lives somewhere else
    HERE = getattr(sys, "_MEIPASS", os.path.dirname(sys.executable))
    APP_DIR = os.path.dirname(sys.executable)
else:
    HERE = APP_DIR = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

try:
    import flashbang as fb
except ImportError:
    sys.stderr.write("flashbang.py must sit next to this script.\n")
    raise

TARGETS = ("graphics", "sound", "logic")

# --- palette ---------------------------------------------------------------
BG = "#15161a"
BG_PANEL = "#1c1e24"
BG_INPUT = "#0f1013"
FG = "#d7dae0"
FG_DIM = "#7c8290"
ACCENT = {"graphics": "#5aa9ff", "sound": "#ffb454", "logic": "#7ee787"}
DANGER = "#ff5f56"


RUFFLE_NAMES = ("ruffle.exe", "ruffle", "ruffle_desktop", "ruffle-nightly",
                "Ruffle")


def find_ruffle():
    """Locate a Ruffle desktop player. Returns a command list, or None."""
    override = os.environ.get("FLASHBANG_RUFFLE")
    if override and os.path.exists(override):
        return [override]
    for name in RUFFLE_NAMES:                  # dropped next to the exe
        local = os.path.join(APP_DIR, name)
        if os.path.isfile(local):
            return [local]
    for name in RUFFLE_NAMES:
        found = shutil.which(name)
        if found:
            return [found]
    if sys.platform == "darwin":
        for app in ("/Applications/Ruffle.app/Contents/MacOS/ruffle",
                    os.path.expanduser(
                        "~/Applications/Ruffle.app/Contents/MacOS/ruffle")):
            if os.path.exists(app):
                return [app]
    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            r = subprocess.run([flatpak, "info", "rs.ruffle.Ruffle"],
                               stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=6)
            if r.returncode == 0:
                return [flatpak, "run", "rs.ruffle.Ruffle"]
        except (OSError, subprocess.SubprocessError):
            pass
    return None


def stray_ruffle_pids():
    """PIDs of Ruffle processes we did not start (best effort, no psutil)."""
    pids = []
    if os.path.isdir("/proc"):
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open("/proc/%s/comm" % entry) as fh:
                    comm = fh.read().strip()
            except OSError:
                continue
            if comm.lower().startswith("ruffle"):
                pids.append(int(entry))
    return pids


class FlashbangGUI:
    def __init__(self, root, initial=None):
        self.root = root
        self.q = queue.Queue()
        self.busy = False
        self.analysis = None          # {target: total_bytes}
        self.ruffle_cmd = None        # resolved lazily
        self.ruffle_proc = None       # the instance we launched

        root.title("Flashbang")
        for ico in (os.path.join(HERE, "flashbang.ico"),
                    os.path.join(APP_DIR, "flashbang.ico")):
            if os.path.isfile(ico):
                try:
                    root.iconbitmap(ico)
                except tk.TclError:
                    pass                       # non-Windows Tk: ignore
                break
        root.configure(bg=BG)
        root.minsize(700, 560)

        self._style()
        self._build()

        if initial:
            self.in_var.set(initial)
            self._autofill_output()
            self.root.after(120, self.analyse)

        self.root.after(80, self._drain)

    # -- chrome -------------------------------------------------------------

    def _style(self):
        s = ttk.Style()
        try:
            s.theme_use("clam")
        except tk.TclError:
            pass
        s.configure(".", background=BG, foreground=FG,
                    fieldbackground=BG_INPUT, bordercolor="#2a2d36",
                    lightcolor=BG_PANEL, darkcolor=BG_PANEL)
        s.configure("TFrame", background=BG)
        s.configure("Panel.TFrame", background=BG_PANEL)
        s.configure("TLabel", background=BG, foreground=FG)
        s.configure("Panel.TLabel", background=BG_PANEL, foreground=FG)
        s.configure("Dim.TLabel", background=BG, foreground=FG_DIM)
        s.configure("PanelDim.TLabel", background=BG_PANEL, foreground=FG_DIM)
        s.configure("Head.TLabel", background=BG, foreground=FG_DIM,
                    font=("TkDefaultFont", 8, "bold"))
        s.configure("TEntry", fieldbackground=BG_INPUT, foreground=FG,
                    insertcolor=FG, borderwidth=1)
        s.configure("TCheckbutton", background=BG_PANEL, foreground=FG,
                    focuscolor=BG_PANEL, indicatorbackground=BG_INPUT,
                    indicatorforeground=DANGER, bordercolor="#333744",
                    padding=2)
        s.map("TCheckbutton",
              background=[("active", BG_PANEL)],
              indicatorbackground=[("selected", DANGER),
                                   ("active", "#22252d"),
                                   ("!selected", BG_INPUT)],
              indicatorforeground=[("selected", BG)])
        s.configure("TButton", background="#2a2d36", foreground=FG,
                    borderwidth=0, padding=(12, 6), focuscolor="#2a2d36")
        s.map("TButton", background=[("active", "#363a45"),
                                     ("disabled", "#22242b")],
              foreground=[("disabled", FG_DIM)])
        s.configure("Go.TButton", background=DANGER, foreground="#15161a",
                    font=("TkDefaultFont", 10, "bold"), padding=(18, 8))
        s.map("Go.TButton", background=[("active", "#ff7a72"),
                                        ("disabled", "#4a2320")],
              foreground=[("disabled", "#8a5450")])
        s.configure("TCombobox", fieldbackground=BG_INPUT, background="#2a2d36",
                    foreground=FG, arrowcolor=FG, borderwidth=0)
        s.map("TCombobox",
              fieldbackground=[("readonly", BG_INPUT)],
              foreground=[("readonly", FG)],
              background=[("readonly", "#2a2d36"), ("active", "#363a45")],
              selectbackground=[("readonly", BG_INPUT)],
              selectforeground=[("readonly", FG)])
        s.configure("Vertical.TScrollbar", background="#2a2d36",
                    troughcolor=BG_INPUT, bordercolor=BG_INPUT,
                    arrowcolor=FG_DIM, borderwidth=0)
        s.map("Vertical.TScrollbar", background=[("active", "#3a3e49")])
        s.configure("Horizontal.TProgressbar", background=DANGER,
                    troughcolor=BG_INPUT, borderwidth=0)
        s.configure("Link.TButton", background=BG, foreground=FG_DIM,
                    borderwidth=0, padding=(4, 0), focuscolor=BG,
                    font=("TkDefaultFont", 8))
        s.map("Link.TButton", background=[("active", BG)],
              foreground=[("active", FG)])
        for t in TARGETS:
            s.configure("%s.Horizontal.TScale" % t, background=BG_PANEL,
                        troughcolor=BG_INPUT)
        s.configure("simple.Horizontal.TScale", background=BG_PANEL,
                    troughcolor=BG_INPUT)

    def _build(self):
        pad = dict(padx=14)
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(6, weight=1)

        # header
        head = ttk.Frame(root)
        head.grid(row=0, column=0, sticky="ew", pady=(12, 6), **pad)
        tk.Label(head, text="FLASHBANG", bg=BG, fg=DANGER,
                 font=("TkDefaultFont", 15, "bold")).pack(side="left")
        tk.Label(head, text="  structure-aware SWF corruption  v%s" % fb.VERSION,
                 bg=BG, fg=FG_DIM).pack(side="left")

        # files
        files = ttk.Frame(root)
        files.grid(row=1, column=0, sticky="ew", pady=4, **pad)
        files.columnconfigure(1, weight=1)
        self.in_var = tk.StringVar()
        self.out_var = tk.StringVar()
        ttk.Label(files, text="Input", style="Dim.TLabel", width=7)\
            .grid(row=0, column=0, sticky="w", pady=3)
        ttk.Entry(files, textvariable=self.in_var)\
            .grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(files, text="Browse", command=self.pick_input)\
            .grid(row=0, column=2, pady=(0, 2))
        ttk.Label(files, text="Output", style="Dim.TLabel", width=7)\
            .grid(row=1, column=0, sticky="w", pady=3)
        ttk.Entry(files, textvariable=self.out_var)\
            .grid(row=1, column=1, sticky="ew", padx=6)
        ttk.Button(files, text="Browse", command=self.pick_output)\
            .grid(row=1, column=2, pady=(2, 0))

        # targets
        thead = ttk.Frame(root)
        thead.grid(row=2, column=0, sticky="ew", pady=(12, 4), **pad)
        ttk.Label(thead, text="TARGETS", style="Head.TLabel").pack(side="left")
        self.mode_var = tk.StringVar(value="simple")
        self.btn_mode = ttk.Button(thead, style="Link.TButton",
                                   text="Advanced \u25b8",
                                   command=self.toggle_mode)
        self.btn_mode.pack(side="right")

        self.on = {}
        self.strength = {}
        self.est = {}

        # -- simple: one dial driving all three targets
        self.simple_panel = ttk.Frame(root, style="Panel.TFrame")
        self.simple_panel.columnconfigure(1, weight=1)
        self.simple_strength = tk.DoubleVar(value=25)
        tk.Label(self.simple_panel, text="Corruption", bg=BG_PANEL, fg=FG,
                 width=11, anchor="w", font=("TkDefaultFont", 9, "bold"))\
            .grid(row=0, column=0, sticky="w", padx=(12, 0), pady=12)
        ttk.Scale(self.simple_panel, from_=0, to=100, orient="horizontal",
                  variable=self.simple_strength,
                  style="simple.Horizontal.TScale",
                  command=lambda _v: self._on_simple_slide())\
            .grid(row=0, column=1, sticky="ew", padx=10)
        self.simple_val = tk.Label(self.simple_panel, text="25", bg=BG_PANEL,
                                   fg=FG, width=4, anchor="e",
                                   font=("TkFixedFont", 10))
        self.simple_val.grid(row=0, column=2)
        self.simple_est = tk.Label(self.simple_panel, text="", bg=BG_PANEL,
                                   fg=FG_DIM, width=17, anchor="w",
                                   font=("TkFixedFont", 8))
        self.simple_est.grid(row=0, column=3, padx=(8, 10))
        tk.Label(self.simple_panel,
                 text="graphics, sound and logic together",
                 bg=BG_PANEL, fg=FG_DIM, anchor="w")\
            .grid(row=1, column=0, columnspan=4, sticky="w",
                  padx=12, pady=(0, 10))

        # -- advanced: a dial per target
        self.adv_panel = ttk.Frame(root, style="Panel.TFrame")
        self.adv_panel.columnconfigure(2, weight=1)
        for i, t in enumerate(TARGETS):
            self.on[t] = tk.BooleanVar(value=True)
            self.strength[t] = tk.DoubleVar(value=[30, 25, 20][i])
            ttk.Checkbutton(self.adv_panel, variable=self.on[t],
                            command=self._refresh_estimates)\
                .grid(row=i, column=0, padx=(10, 2), pady=8)
            tk.Label(self.adv_panel, text=t.capitalize(), bg=BG_PANEL,
                     fg=ACCENT[t], width=9, anchor="w",
                     font=("TkDefaultFont", 9, "bold"))\
                .grid(row=i, column=1, sticky="w")
            ttk.Scale(self.adv_panel, from_=0, to=100, orient="horizontal",
                      variable=self.strength[t],
                      style="%s.Horizontal.TScale" % t,
                      command=lambda _v, tt=t: self._on_slide(tt))\
                .grid(row=i, column=2, sticky="ew", padx=10)
            val = tk.Label(self.adv_panel, text="30", bg=BG_PANEL, fg=FG,
                           width=4, anchor="e", font=("TkFixedFont", 10))
            val.grid(row=i, column=3)
            est = tk.Label(self.adv_panel, text="", bg=BG_PANEL, fg=FG_DIM,
                           width=17, anchor="w", font=("TkFixedFont", 8))
            est.grid(row=i, column=4, padx=(8, 10))
            self.est[t] = (val, est)
            self._on_slide(t)
        self._apply_mode()

        # options
        ttk.Label(root, text="OPTIONS", style="Head.TLabel")\
            .grid(row=4, column=0, sticky="w", pady=(14, 4), **pad)
        opt = ttk.Frame(root, style="Panel.TFrame")
        opt.grid(row=5, column=0, sticky="ew", **pad)

        self.seed_var = tk.StringVar(value=str(random.randrange(1 << 30)))
        self.wild_var = tk.BooleanVar(value=False)
        self.comp_var = tk.StringVar(value="keep")

        r0 = ttk.Frame(opt, style="Panel.TFrame")
        r0.pack(fill="x", padx=10, pady=10)
        ttk.Label(r0, text="Seed", style="PanelDim.TLabel").pack(side="left")
        ttk.Entry(r0, textvariable=self.seed_var, width=12)\
            .pack(side="left", padx=6)
        ttk.Button(r0, text="New", width=5, command=self.new_seed)\
            .pack(side="left")
        ttk.Label(r0, text="   Compress", style="PanelDim.TLabel")\
            .pack(side="left")
        ttk.Combobox(r0, textvariable=self.comp_var, width=6, state="readonly",
                     values=("keep", "yes", "no")).pack(side="left", padx=6)
        ttk.Checkbutton(r0, text="Wild mode", variable=self.wild_var,
                        command=self._wild_changed).pack(side="left",
                                                         padx=(16, 0))
        self.ruffle_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(r0, text="Open in Ruffle", variable=self.ruffle_var,
                        command=self._ruffle_toggled).pack(side="left",
                                                           padx=(16, 0))

        # log
        logwrap = ttk.Frame(root)
        logwrap.grid(row=6, column=0, sticky="nsew", pady=(14, 4), **pad)
        logwrap.rowconfigure(0, weight=1)
        logwrap.columnconfigure(0, weight=1)
        self.log = tk.Text(logwrap, bg=BG_INPUT, fg=FG, bd=0, wrap="none",
                           highlightthickness=0, insertbackground=FG, height=12,
                           font=("TkFixedFont", 9), padx=10, pady=8)
        self.log.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(logwrap, command=self.log.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.log.configure(yscrollcommand=sb.set, state="disabled")
        for t in TARGETS:
            self.log.tag_configure(t, foreground=ACCENT[t])
        self.log.tag_configure("dim", foreground=FG_DIM)
        self.log.tag_configure("bad", foreground=DANGER)
        self.log.tag_configure("good", foreground="#7ee787")

        # actions
        bar = ttk.Frame(root)
        bar.grid(row=7, column=0, sticky="ew", pady=(4, 14), **pad)
        self.prog = ttk.Progressbar(bar, mode="indeterminate", length=140)
        self.btn_analyse = ttk.Button(bar, text="Analyse", command=self.analyse)
        self.btn_analyse.pack(side="left")
        self.btn_random = ttk.Button(bar, text="Randomize",
                                     command=self.randomize)
        self.btn_random.pack(side="left", padx=6)
        ttk.Button(bar, text="Clear log", command=self.clear_log)\
            .pack(side="left")
        self.btn_go = ttk.Button(bar, text="Flashbang it", style="Go.TButton",
                                 command=self.corrupt)
        self.btn_go.pack(side="right")

        self.in_var.trace_add("write", lambda *_: self._input_changed())
        self.root.bind("<Control-r>", lambda _e: self.randomize())
        self.root.bind("<Control-Return>", lambda _e: self.corrupt())
        self.write("Pick a .swf and hit Analyse to see what can be broken.\n"
                   "Randomize (Ctrl+R) rolls a set of settings for you.\n",
                   "dim")

    # -- small helpers ------------------------------------------------------

    def write(self, text, tag=None):
        self.log.configure(state="normal")
        self.log.insert("end", text, tag or ())
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def new_seed(self):
        self.seed_var.set(str(random.randrange(1 << 30)))

    @property
    def simple(self):
        return self.mode_var.get() == "simple"

    def toggle_mode(self):
        if self.simple:
            # carry the single dial across so nothing jumps
            for t in TARGETS:
                self.on[t].set(True)
                self.strength[t].set(round(self.simple_strength.get()))
            self.mode_var.set("advanced")
        else:
            live = [self.strength[t].get() for t in TARGETS
                    if self.on[t].get()]
            if live:
                self.simple_strength.set(round(sum(live) / len(live)))
            self.mode_var.set("simple")
        self._apply_mode()

    def _apply_mode(self):
        pad = dict(padx=14)
        if self.simple:
            self.adv_panel.grid_forget()
            self.simple_panel.grid(row=3, column=0, sticky="ew", **pad)
            self.btn_mode.configure(text="Advanced \u25b8")
        else:
            self.simple_panel.grid_forget()
            self.adv_panel.grid(row=3, column=0, sticky="ew", **pad)
            self.btn_mode.configure(text="\u25c2 Simple")
        for t in TARGETS:
            self._on_slide(t)
        self._on_simple_slide()

    def _on_simple_slide(self):
        val = int(round(self.simple_strength.get()))
        self.simple_strength.set(val)
        self.simple_val.configure(text=str(val))
        self._refresh_estimates()

    def _wild_changed(self):
        # wild mode changes which regions exist, so the analysis is stale
        self.analysis = None
        self._refresh_estimates()
        if os.path.isfile(self.in_var.get().strip()):
            self.analyse()

    def _input_changed(self):
        self.analysis = None
        self._refresh_estimates()

    def _on_slide(self, t):
        if t not in self.strength:
            return
        val = int(round(self.strength[t].get()))
        self.strength[t].set(val)
        self.est[t][0].configure(text=str(val))
        self._refresh_estimates()

    def _refresh_estimates(self):
        if self.simple:
            if not self.analysis:
                self.simple_est.configure(text="")
            else:
                size = sum(self.analysis.values())
                hits = sum(v * fb.strength_to_p(self.simple_strength.get(), t)
                           for t, v in self.analysis.items())
                if not size:
                    self.simple_est.configure(text="nothing found")
                else:
                    shown = ("%.1f" % hits if hits < 10
                             else "{:,}".format(round(hits)))
                    self.simple_est.configure(
                        text="~%s of %s B" % (shown, "{:,}".format(size)))
        for t in TARGETS:
            if t not in self.est:
                continue          # still building the widget tree
            _, lbl = self.est[t]
            if not self.on[t].get():
                lbl.configure(text="off")
                continue
            if not self.analysis:
                lbl.configure(text="")
                continue
            size = self.analysis.get(t, 0)
            if not size:
                lbl.configure(text="nothing found")
                continue
            p = fb.strength_to_p(self.strength[t].get(), t)
            hits = size * p
            shown = "%.1f" % hits if hits < 10 else "{:,}".format(round(hits))
            lbl.configure(text="~%s of %s B" % (shown, "{:,}".format(size)))

    def randomize(self):
        """Roll the dials.

        Simple mode rolls the one slider; advanced mode switches on a random
        subset of targets - always at least one - and rolls each separately.
        """
        if self.busy:
            return
        rng = random.Random()

        if self.simple:
            level = rng.randint(1, 100)
            self.simple_strength.set(level)
            self._on_simple_slide()
            seed = rng.randrange(1 << 30)
            self.seed_var.set(str(seed))
            self.write("\nroll: corruption=%d | seed %d | wild %s\n"
                       % (level, seed,
                          "on" if self.wild_var.get() else "off"), "good")
            return

        # only offer targets this file can actually reach
        if self.analysis:
            usable = [t for t in TARGETS if self.analysis.get(t)]
        else:
            usable = list(TARGETS)
        if not usable:
            self.write("nothing corruptible in this file to randomize\n", "bad")
            return

        chosen = [t for t in usable if rng.random() < 0.5]
        if not chosen:                       # never roll a no-op
            chosen = [rng.choice(usable)]

        for t in TARGETS:
            on = t in chosen
            self.on[t].set(on)
            if on:
                self.strength[t].set(rng.randint(1, 100))
            self._on_slide(t)

        seed = rng.randrange(1 << 30)
        self.seed_var.set(str(seed))

        detail = " ".join(
            "%s=%s" % (t, int(self.strength[t].get()) if t in chosen else "off")
            for t in TARGETS)
        self.write("\nroll: %s | seed %d | wild %s\n"
                   % (detail, seed, "on" if self.wild_var.get() else "off"),
                   "good")

    def _ruffle_toggled(self):
        """Resolve the Ruffle binary the first time the box is ticked."""
        if not self.ruffle_var.get():
            return
        if self.ruffle_cmd:
            return
        self.ruffle_cmd = find_ruffle()
        if self.ruffle_cmd:
            self.write("ruffle: %s\n" % " ".join(self.ruffle_cmd), "dim")
            return
        path = filedialog.askopenfilename(
            title="Where is Ruffle? (cancel to switch this off)")
        if path:
            self.ruffle_cmd = [path]
            self.write("ruffle: %s\n" % path, "dim")
        else:
            self.ruffle_var.set(False)
            self.write("ruffle not found - set FLASHBANG_RUFFLE or pick it "
                       "manually\n", "bad")

    def _close_ruffle(self):
        """Shut down any running Ruffle so the new file opens on its own."""
        killed = 0
        proc = self.ruffle_proc
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass
            killed += 1
        self.ruffle_proc = None

        mine = proc.pid if proc else None
        for pid in stray_ruffle_pids():
            if pid == mine or pid == os.getpid():
                continue
            try:
                os.kill(pid, 15)
                killed += 1
            except OSError:
                pass
        if not os.path.isdir("/proc"):
            # no /proc to enumerate, so sweep by name regardless of whether we
            # already closed our own child - other windows must go too
            cmd = (["taskkill", "/F", "/IM", "ruffle.exe"]
                   if os.name == "nt" else ["pkill", "-x", "ruffle"])
            try:
                r = subprocess.run(cmd, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=5)
                if r.returncode == 0 and killed == 0:
                    killed = 1
            except (OSError, subprocess.SubprocessError):
                pass
        return killed

    def _open_in_ruffle(self, path):
        if not self.ruffle_cmd:
            self.ruffle_cmd = find_ruffle()
        if not self.ruffle_cmd:
            self.write("ruffle not found - nothing launched\n", "bad")
            return
        killed = self._close_ruffle()
        if killed:
            self.write("  closed %d running Ruffle instance%s\n"
                       % (killed, "" if killed == 1 else "s"), "dim")
        try:
            self.ruffle_proc = subprocess.Popen(
                self.ruffle_cmd + [os.path.abspath(path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=(os.name != "nt"))
            self.write("  launched in Ruffle (pid %d)\n"
                       % self.ruffle_proc.pid, "dim")
        except OSError as exc:
            self.write("  could not start Ruffle: %s\n" % exc, "bad")

    def pick_input(self):
        path = filedialog.askopenfilename(
            title="Pick a Flash movie",
            filetypes=[("Flash movie", "*.swf"), ("All files", "*.*")])
        if path:
            self.in_var.set(path)
            self._autofill_output()
            self.analyse()

    def pick_output(self):
        path = filedialog.asksaveasfilename(
            title="Save corrupted movie as", defaultextension=".swf",
            filetypes=[("Flash movie", "*.swf")])
        if path:
            self.out_var.set(path)

    def _autofill_output(self):
        src = self.in_var.get().strip()
        if src:
            stem, ext = os.path.splitext(src)
            self.out_var.set("%s_flashbanged%s" % (stem, ext or ".swf"))

    def _collect(self):
        src = self.in_var.get().strip()
        if not src:
            raise ValueError("no input file selected")
        if not os.path.isfile(src):
            raise ValueError("input file does not exist")
        if self.simple:
            targets = set(TARGETS)
        else:
            targets = {t for t in TARGETS if self.on[t].get()}
            if not targets:
                raise ValueError("every target is switched off")
        try:
            seed = int(self.seed_var.get().strip())
        except ValueError:
            raise ValueError("seed must be a whole number")
        if self.simple:
            level = self.simple_strength.get()
            strengths = {t: level for t in TARGETS}
        else:
            strengths = {t: (self.strength[t].get() if t in targets else 0.0)
                         for t in TARGETS}
        return src, targets, strengths, seed

    # -- work ---------------------------------------------------------------

    def _start(self, fn):
        if self.busy:
            return
        self.busy = True
        self.btn_go.state(["disabled"])
        self.btn_analyse.state(["disabled"])
        self.btn_random.state(["disabled"])
        self.prog.pack(side="right", padx=10)
        self.prog.start(12)
        threading.Thread(target=self._wrap, args=(fn,), daemon=True).start()

    def _wrap(self, fn):
        try:
            fn()
        except Exception as exc:
            self.q.put(("log", ("error: %s\n" % exc, "bad")))
            if not isinstance(exc, ValueError):
                self.q.put(("log", (traceback.format_exc(), "dim")))
        finally:
            self.q.put(("done", None))

    def _drain(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "log":
                    self.write(*payload) if isinstance(payload, tuple) \
                        else self.write(payload)
                elif kind == "analysis":
                    self.analysis = payload
                    self._refresh_estimates()
                elif kind == "open":
                    self._open_in_ruffle(payload)
                elif kind == "done":
                    self.busy = False
                    self.btn_go.state(["!disabled"])
                    self.btn_analyse.state(["!disabled"])
                    self.btn_random.state(["!disabled"])
                    self.prog.stop()
                    self.prog.pack_forget()
        except queue.Empty:
            pass
        self.root.after(80, self._drain)

    def analyse(self):
        try:
            src = self.in_var.get().strip()
            if not src or not os.path.isfile(src):
                raise ValueError("pick an existing .swf first")
        except ValueError as exc:
            self.write("error: %s\n" % exc, "bad")
            return
        wild = self.wild_var.get()
        self._start(lambda: self._do_analyse(src, wild))

    def _do_analyse(self, src, wild):
        swf = fb.Swf.load(src)
        scanner = fb.Scanner(swf, set(TARGETS), wild=wild)
        regions = scanner.scan()
        put = self.q.put
        put(("log", ("\n%s\n" % os.path.basename(src), "good")))
        put(("log", ("  %s v%d, %d bytes uncompressed, %d tags\n" % (
            swf.signature.decode(), swf.version, len(swf.body) + 8,
            sum(scanner.tag_counts.values())), "dim")))
        top = sorted(scanner.tag_counts.items(), key=lambda kv: -kv[1])[:6]
        put(("log", ("  %s\n" % ", ".join(
            "%s x%d" % (fb.TAG_NAMES.get(c, "Tag%d" % c), n) for c, n in top),
            "dim")))
        totals = {}
        counts = {}
        for r in regions:
            totals[r.target] = totals.get(r.target, 0) + r.size
            counts[r.target] = counts.get(r.target, 0) + 1
        for t in TARGETS:
            if totals.get(t):
                put(("log", ("  %-9s %5d regions  %8d bytes\n"
                             % (t, counts[t], totals[t]), t)))
            else:
                put(("log", ("  %-9s nothing corruptible\n" % t, "dim")))
        if scanner.skipped:
            put(("log", ("  %d tag(s) unparseable, left intact\n"
                         % len(scanner.skipped), "dim")))
        put(("analysis", totals))

    def corrupt(self):
        try:
            src, targets, strengths, seed = self._collect()
        except ValueError as exc:
            messagebox.showerror("Flashbang", str(exc))
            return
        out = self.out_var.get().strip()
        if not out:
            self._autofill_output()
            out = self.out_var.get().strip()
        if os.path.abspath(out) == os.path.abspath(src):
            messagebox.showerror("Flashbang",
                                 "output would overwrite the input file")
            return
        wild = self.wild_var.get()
        comp = {"keep": None, "yes": True, "no": False}[self.comp_var.get()]
        open_after = self.ruffle_var.get()
        self._start(lambda: self._do_corrupt(src, out, targets, strengths,
                                             seed, wild, comp, open_after))

    def _do_corrupt(self, src, out, targets, strengths, seed, wild, comp,
                    open_after=False):
        put = self.q.put
        put(("log", ("\nseed %d%s | %s\n" % (
            seed, "  wild" if wild else "", ", ".join(sorted(targets))),
            "dim")))
        swf = fb.Swf.load(src)
        scanner = fb.Scanner(swf, targets, wild=wild)
        regions = scanner.scan()
        rng = random.Random(seed)
        stats = fb.Corruptor(swf.body, rng, strengths, wild=wild).run(regions)
        written = swf.save(out, compress=comp)
        put(("log", ("  %s  " % os.path.basename(out), "good")))
        detail = "  ".join("%s %d" % (t, stats[t])
                           for t in TARGETS if stats.get(t))
        put(("log", ("%d B   %s\n" % (written, detail or "no hits"), "dim")))
        put(("log", ("done - same seed reproduces this exactly\n", "dim")))
        if open_after:
            put(("open", out))


def main():
    initial = sys.argv[1] if len(sys.argv) > 1 else None
    root = tk.Tk()
    FlashbangGUI(root, initial)
    root.mainloop()


if __name__ == "__main__":
    main()
