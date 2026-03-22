import os
import json
import base64
import shutil
import ftplib
import logging
import threading
import collections
import xml.etree.ElementTree as ET
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from datetime import datetime

_APP_DIR     = os.path.dirname(os.path.abspath(__file__))
LOG_FILE     = os.path.join(_APP_DIR, "file_transfer.log")
CONFIG_FILE  = os.path.join(_APP_DIR, "file_transfer_config.json")
HISTORY_MAX    = 5
_FZ_DIR        = os.path.join(os.environ.get("APPDATA", ""),  "FileZilla")
FZ_SITE_XML    = os.path.join(_FZ_DIR, "sitemanager.xml")    
FZ_RECENT_XML  = os.path.join(_FZ_DIR, "recentservers.xml")  

_xlog = logging.getLogger("xfer")  
_flog = logging.getLogger("ftp")   

class _DequeHandler(logging.Handler):
    """Appends formatted log records to an in-memory deque for UI display."""
    def __init__(self, deque_ref):
        super().__init__()
        self._deque = deque_ref

    def emit(self, record):
        self._deque.append(self.format(record))


def setup_logging(xfer_deque, ftp_deque):
    fmt = "%(asctime)s - %(levelname)s - %(message)s"

    # Root logger → log file (receives everything via propagation)
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    if not any(isinstance(h, logging.FileHandler) for h in root.handlers):
        fh = logging.FileHandler(LOG_FILE, encoding="utf-8")
        fh.setFormatter(logging.Formatter(fmt))
        root.addHandler(fh)

    # xfer logger → xfer_deque  (Tab 1 display)
    xlog = logging.getLogger("xfer")
    xlog.setLevel(logging.INFO)
    xlog.propagate = True
    if not xlog.handlers:
        h = _DequeHandler(xfer_deque)
        h.setFormatter(logging.Formatter(fmt))
        xlog.addHandler(h)

    # ftp logger → ftp_deque  (Tab 2 display)
    flog = logging.getLogger("ftp")
    flog.setLevel(logging.INFO)
    flog.propagate = True
    if not flog.handlers:
        h = _DequeHandler(ftp_deque)
        h.setFormatter(logging.Formatter(fmt))
        flog.addHandler(h)


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _fmt_size(nbytes):
    if nbytes < 1_024:
        return f"{nbytes} B"
    elif nbytes < 1_024 ** 2:
        return f"{nbytes / 1_024:.1f} KB"
    return f"{nbytes / 1_024 ** 2:.1f} MB"


def _fmt_mtime(mtime):
    return datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M")


# ---------------------------------------------------------------------------
# FileZilla sitemanager.xml parser
# ---------------------------------------------------------------------------

def _parse_fz_element(elem, folder_prefix=""):
    """
    Recursively walk <Servers> / <Folder> / <Server> elements and return a
    flat list of site dicts.  Folder names are prepended to site names so
    the dropdown shows  "Production / Main Server".
    """
    sites = []
    for child in elem:

        if child.tag == "Server":
            raw_name  = (child.findtext("Name") or "").strip() or "Unnamed Site"
            full_name = f"{folder_prefix}{raw_name}" if folder_prefix else raw_name
            host      = (child.findtext("Host") or "").strip()
            port      = int(child.findtext("Port") or "21")
            user      = (child.findtext("User") or "").strip()
            protocol  = int(child.findtext("Protocol") or "0")

            password  = _decode_fz_password(child.find("Pass"))
            proto_str = _proto_str(protocol)

            sites.append({
                "name":         full_name,
                "host":         host,
                "port":         port,
                "user":         user,
                "password":     password,
                "protocol":     protocol,
                "protocol_str": proto_str,
            })

        elif child.tag == "Folder":
            folder_name = (child.findtext("Name") or "").strip()
            new_prefix  = f"{folder_prefix}{folder_name} / " if folder_name else folder_prefix
            sites.extend(_parse_fz_element(child, new_prefix))

    return sites


def _decode_fz_password(pass_el):
    """Decode a FileZilla <Pass> element — handles plain text and base64."""
    if pass_el is None or not pass_el.text:
        return ""
    if pass_el.get("encoding", "plain") == "base64":
        try:
            return base64.b64decode(pass_el.text).decode("utf-8")
        except Exception:
            return ""
    return pass_el.text or ""


def _proto_str(protocol_num):
    return {0: "FTP", 1: "SFTP",
            3: "FTPS (explicit)",
            4: "FTPS (implicit)"}.get(protocol_num, f"Protocol {protocol_num}")


def read_fz_sites():
    """
    Read FTP sites from BOTH FileZilla sources and return a merged list.

      1. sitemanager.xml  — named/bookmarked sites (Site Manager)
      2. recentservers.xml — QuickConnect history  (no saved name, so we build one)

    Sites from Site Manager come first; QuickConnect entries are appended
    if they are not already present (matched by host+user, case-insensitive).

    Returns (list_of_site_dicts, error_string_or_None).
    error is only set when NEITHER file can be found at all.
    """
    all_sites  = []
    parse_errs = []

    # ── 1. Site Manager ───────────────────────────────────────────────
    if os.path.isfile(FZ_SITE_XML):
        try:
            root_el  = ET.parse(FZ_SITE_XML).getroot()
            servers  = root_el.find("Servers")
            if servers is not None:
                all_sites.extend(_parse_fz_element(servers))
        except ET.ParseError as exc:
            parse_errs.append(f"sitemanager.xml: {exc}")
    else:
        parse_errs.append(f"sitemanager.xml not found at:\n  {FZ_SITE_XML}")

    # ── 2. QuickConnect history ───────────────────────────────────────
    if os.path.isfile(FZ_RECENT_XML):
        try:
            root_el  = ET.parse(FZ_RECENT_XML).getroot()
            recent   = root_el.find("RecentServers")
            if recent is not None:
                # Build a set of already-known host+user combos to avoid duplicates
                known = {(s["host"].lower(), s["user"].lower()) for s in all_sites}

                for srv in recent.findall("Server"):
                    host     = (srv.findtext("Host") or "").strip()
                    port     = int(srv.findtext("Port") or "21")
                    user     = (srv.findtext("User") or "").strip()
                    protocol = int(srv.findtext("Protocol") or "0")
                    password = _decode_fz_password(srv.find("Pass"))

                    if (host.lower(), user.lower()) in known:
                        continue   # already listed from Site Manager

                    # No <Name> in QuickConnect — build a readable display name
                    display = f"{host}  ({user})  [QuickConnect]"
                    all_sites.append({
                        "name":         display,
                        "host":         host,
                        "port":         port,
                        "user":         user,
                        "password":     password,
                        "protocol":     protocol,
                        "protocol_str": _proto_str(protocol),
                    })
                    known.add((host.lower(), user.lower()))
        except ET.ParseError as exc:
            parse_errs.append(f"recentservers.xml: {exc}")
    else:
        parse_errs.append(f"recentservers.xml not found at:\n  {FZ_RECENT_XML}")

    # ── Result ────────────────────────────────────────────────────────
    if all_sites:
        return all_sites, None          # success — ignore any minor parse warnings

    # Nothing found at all
    err = (
        "No FTP sites found in FileZilla.\n\n"
        "Checked:\n"
        f"  • {FZ_SITE_XML}\n"
        f"  • {FZ_RECENT_XML}\n\n"
        + ("\n".join(parse_errs) if parse_errs else
           "Both files are present but contain no servers.\n"
           "Please connect to a server in FileZilla first (QuickConnect bar),\n"
           "then click Refresh Sites here.")
    )
    return [], err


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class FileTransferApp:
    def __init__(self, root):
        self.root = root
        self.root.title("File Transfer Utility — Developed by Preeti")
        self.root.geometry("900x720")
        self.root.minsize(660, 560)
        self.root.resizable(True, True)

        # ── Shared state ──────────────────────────────────────────────
        self.source_path    = tk.StringVar()
        self.dest_path      = tk.StringVar()
        self.check_vars     = []                        # (BooleanVar, filename)
        self.file_entries   = []                        # (filename, size_str, mtime_str)
        self.log_lines      = collections.deque(maxlen=300)
        self.ftp_log_lines  = collections.deque(maxlen=300)
        self._busy          = False                     # Tab 1 transfer in progress
        self._ftp_busy      = False                     # Tab 2 FTP in progress
        self._src_history   = []
        self._dst_history   = []
        self._ftp_sites     = []                        # parsed site dicts
        self._last_ftp_site = ""                        # remembered site name

        setup_logging(self.log_lines, self.ftp_log_lines)
        self._build_ui()
        self._load_config()
        # Auto-load FTP sites silently on startup
        self._refresh_ftp_sites(silent=True)

    # ══════════════════════════════════════════════════════════════════
    # Top-level UI — notebook + shared status bar
    # ══════════════════════════════════════════════════════════════════

    def _build_ui(self):
        # Status bar lives outside the notebook so it is always visible
        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self.root, textvariable=self.status_var,
                  relief=tk.SUNKEN, anchor="w").pack(
            fill=tk.X, side=tk.BOTTOM, ipady=2)

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        self.notebook.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        tab1 = ttk.Frame(self.notebook)
        tab2 = ttk.Frame(self.notebook)
        self.notebook.add(tab1, text="   File Transfer   ")
        self.notebook.add(tab2, text="   FTP Upload   ")

        self._build_transfer_tab(tab1)
        self._build_ftp_tab(tab2)

    def _on_tab_changed(self, _event=None):
        """When the user switches to the FTP tab, auto-refresh the Folder B file count."""
        if self.notebook.index(self.notebook.select()) == 1:
            self._scan_folder_b()

    # ══════════════════════════════════════════════════════════════════
    # Tab 1 — File Transfer
    # ══════════════════════════════════════════════════════════════════

    def _build_transfer_tab(self, parent):
        pad = {"padx": 8, "pady": 4}

        # ── Folders ───────────────────────────────────────────────────
        folder_frame = ttk.LabelFrame(parent, text="Folders", padding=6)
        folder_frame.pack(fill=tk.X, **pad)
        folder_frame.columnconfigure(1, weight=1)

        ttk.Label(folder_frame, text="Source folder (A):").grid(
            row=0, column=0, sticky="w", pady=2)
        self.src_combo = ttk.Combobox(folder_frame, textvariable=self.source_path, width=58)
        self.src_combo.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(folder_frame, text="Browse…",
                   command=lambda: self._browse(self.source_path, self.src_combo)
                   ).grid(row=0, column=2)

        ttk.Label(folder_frame, text="Destination folder (B):").grid(
            row=1, column=0, sticky="w", pady=2)
        self.dst_combo = ttk.Combobox(folder_frame, textvariable=self.dest_path, width=58)
        self.dst_combo.grid(row=1, column=1, sticky="ew", padx=4)
        ttk.Button(folder_frame, text="Browse…",
                   command=lambda: self._browse(self.dest_path, self.dst_combo)
                   ).grid(row=1, column=2)

        ttk.Button(folder_frame, text="Load files from source →",
                   command=self.load_files).grid(
            row=2, column=0, columnspan=3, pady=(6, 2))

        # ── File list ─────────────────────────────────────────────────
        list_outer = ttk.LabelFrame(parent, text="Files in source folder", padding=4)
        list_outer.pack(fill=tk.BOTH, expand=True, **pad)

        toolbar = ttk.Frame(list_outer)
        toolbar.pack(fill=tk.X, pady=(0, 4))
        ttk.Button(toolbar, text="Select all",   command=self.select_all  ).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Deselect all", command=self.deselect_all).pack(side=tk.LEFT, padx=2)
        self.sel_label = ttk.Label(toolbar, text="0 of 0 files selected")
        self.sel_label.pack(side=tk.RIGHT, padx=4)

        canvas_frame = ttk.Frame(list_outer)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        self.canvas   = tk.Canvas(canvas_frame, borderwidth=0, highlightthickness=0)
        vscroll       = ttk.Scrollbar(canvas_frame, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=vscroll.set)
        vscroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        self.inner_frame = ttk.Frame(self.canvas)
        self._window_id  = self.canvas.create_window((0, 0), window=self.inner_frame, anchor="nw")
        self.inner_frame.bind("<Configure>", self._on_inner_configure)
        self.canvas.bind("<Configure>",      self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        hdr = ttk.Frame(self.inner_frame)
        hdr.pack(fill=tk.X)
        ttk.Label(hdr, text="  Sel",    width=6,  anchor="w").pack(side=tk.LEFT)
        ttk.Label(hdr, text="Filename", width=42, anchor="w").pack(side=tk.LEFT)
        ttk.Label(hdr, text="Size",     width=10, anchor="e").pack(side=tk.LEFT)
        ttk.Label(hdr, text="Modified", width=18, anchor="w", padding=(6, 0)).pack(side=tk.LEFT)
        ttk.Separator(self.inner_frame, orient="horizontal").pack(fill=tk.X, pady=2)

        self.rows_frame = ttk.Frame(self.inner_frame)
        self.rows_frame.pack(fill=tk.X)

        # ── Action ────────────────────────────────────────────────────
        act = ttk.Frame(parent)
        act.pack(fill=tk.X, **pad)
        self.copy_btn = ttk.Button(act, text="COPY SELECTED FILES TO DESTINATION",
                                   command=self.copy_files)
        self.copy_btn.pack(side=tk.LEFT, padx=4)
        ttk.Label(act, text="  All existing files in destination will be deleted first.",
                  foreground="#c0392b").pack(side=tk.LEFT)

        # ── Log ───────────────────────────────────────────────────────
        log_frm = ttk.LabelFrame(
            parent,
            text="Activity log  (last 12 lines — full history in file_transfer.log)",
            padding=4)
        log_frm.pack(fill=tk.X, **pad)

        self.log_text = tk.Text(log_frm, height=6, state=tk.DISABLED,
                                font=("Consolas", 8), wrap=tk.NONE)
        ls = ttk.Scrollbar(log_frm, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=ls.set)
        ls.pack(side=tk.RIGHT, fill=tk.Y)
        self.log_text.pack(fill=tk.X)

    # ══════════════════════════════════════════════════════════════════
    # Tab 2 — FTP Upload
    # ══════════════════════════════════════════════════════════════════

    def _build_ftp_tab(self, parent):
        pad = {"padx": 8, "pady": 4}

        # ── Site selection ────────────────────────────────────────────
        site_frm = ttk.LabelFrame(parent, text="FTP Site  (from FileZilla Site Manager + QuickConnect history)", padding=6)
        site_frm.pack(fill=tk.X, **pad)
        site_frm.columnconfigure(1, weight=1)

        ttk.Label(site_frm, text="Select site:").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self.site_var   = tk.StringVar()
        self.site_combo = ttk.Combobox(site_frm, textvariable=self.site_var,
                                       state="readonly", width=50)
        self.site_combo.grid(row=0, column=1, sticky="ew", padx=4)
        self.site_combo.bind("<<ComboboxSelected>>", self._on_site_selected)
        ttk.Button(site_frm, text="Refresh Sites",
                   command=self._refresh_ftp_sites).grid(row=0, column=2, padx=(4, 0))

        # ── Connection details ────────────────────────────────────────
        info_frm = ttk.LabelFrame(parent, text="Connection Details", padding=8)
        info_frm.pack(fill=tk.X, **pad)
        info_frm.columnconfigure(1, weight=2)
        info_frm.columnconfigure(3, weight=1)

        def _lbl(row, col, text):
            ttk.Label(info_frm, text=text).grid(
                row=row, column=col, sticky="w", padx=(0, 6), pady=2)

        def _val(row, col, var, colspan=1):
            ttk.Label(info_frm, textvariable=var, foreground="#1a5276").grid(
                row=row, column=col, columnspan=colspan, sticky="w", pady=2)

        self.ftp_host_var      = tk.StringVar(value="—")
        self.ftp_port_var      = tk.StringVar(value="—")
        self.ftp_user_var      = tk.StringVar(value="—")
        self.ftp_proto_var     = tk.StringVar(value="—")
        self.ftp_remotedir_var = tk.StringVar(value="(shown after first connect)")

        _lbl(0, 0, "Host:");      _val(0, 1, self.ftp_host_var)
        _lbl(0, 2, "Port:");      _val(0, 3, self.ftp_port_var)
        _lbl(1, 0, "Username:");  _val(1, 1, self.ftp_user_var)
        _lbl(1, 2, "Protocol:");  _val(1, 3, self.ftp_proto_var)
        _lbl(2, 0, "Remote dir:"); _val(2, 1, self.ftp_remotedir_var, colspan=3)

        # ── Upload source (Folder B) ───────────────────────────────────
        src_frm = ttk.LabelFrame(parent, text="Upload Source  (Folder B set in Tab 1)", padding=6)
        src_frm.pack(fill=tk.X, **pad)
        src_frm.columnconfigure(1, weight=1)

        ttk.Label(src_frm, text="Folder B:").grid(row=0, column=0, sticky="w", padx=(0, 6), pady=2)
        ttk.Label(src_frm, textvariable=self.dest_path,
                  foreground="#1a5276", anchor="w").grid(row=0, column=1, sticky="ew")

        ttk.Label(src_frm, text="Files ready:").grid(row=1, column=0, sticky="w", padx=(0, 6), pady=2)
        self.ftp_filecount_var = tk.StringVar(value="— (set Folder B in Tab 1 first)")
        ttk.Label(src_frm, textvariable=self.ftp_filecount_var,
                  foreground="#1a5276").grid(row=1, column=1, sticky="w")
        ttk.Button(src_frm, text="Scan",
                   command=self._scan_folder_b).grid(row=1, column=2, padx=(6, 0))

        # ── Action ────────────────────────────────────────────────────
        act = ttk.Frame(parent)
        act.pack(fill=tk.X, **pad)
        self.ftp_btn = ttk.Button(act, text="UPLOAD FOLDER B TO FTP SERVER",
                                  command=self.ftp_upload)
        self.ftp_btn.pack(side=tk.LEFT, padx=4)
        ttk.Label(act, text="  All existing remote files will be deleted first.",
                  foreground="#c0392b").pack(side=tk.LEFT)

        # ── FTP Log ───────────────────────────────────────────────────
        ftp_log_frm = ttk.LabelFrame(
            parent,
            text="FTP Activity Log  (last 15 lines — full history in file_transfer.log)",
            padding=4)
        ftp_log_frm.pack(fill=tk.BOTH, expand=True, **pad)

        self.ftp_log_text = tk.Text(ftp_log_frm, height=10, state=tk.DISABLED,
                                    font=("Consolas", 8), wrap=tk.NONE)
        fls = ttk.Scrollbar(ftp_log_frm, orient="vertical", command=self.ftp_log_text.yview)
        self.ftp_log_text.configure(yscrollcommand=fls.set)
        fls.pack(side=tk.RIGHT, fill=tk.Y)
        self.ftp_log_text.pack(fill=tk.BOTH, expand=True)

    # ══════════════════════════════════════════════════════════════════
    # Canvas / scroll (Tab 1)
    # ══════════════════════════════════════════════════════════════════

    def _on_inner_configure(self, _event):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfig(self._window_id, width=event.width)

    def _on_mousewheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    # ══════════════════════════════════════════════════════════════════
    # Tab 1 — folder browsing & file transfer logic
    # ══════════════════════════════════════════════════════════════════

    def _browse(self, var, combo):
        initial = var.get() if os.path.isdir(var.get()) else "/"
        path = filedialog.askdirectory(title="Select folder", initialdir=initial)
        if path:
            var.set(os.path.normpath(path))
            self._record_path(var, combo)
            self._save_config()

    def load_files(self):
        src = self.source_path.get().strip()
        if not src:
            messagebox.showwarning("No source", "Please enter or browse to a source folder first.")
            return
        if not os.path.isdir(src):
            messagebox.showerror("Invalid path", f"Source folder not found:\n{src}")
            return
        try:
            entries = []
            for name in sorted(os.listdir(src)):
                full = os.path.join(src, name)
                if os.path.isfile(full):
                    st = os.stat(full)
                    entries.append((name, _fmt_size(st.st_size), _fmt_mtime(st.st_mtime)))
        except PermissionError as exc:
            messagebox.showerror("Access denied", str(exc))
            return
        except OSError as exc:
            messagebox.showerror("Error reading folder", str(exc))
            return

        self.file_entries = entries
        self._refresh_file_list()
        self.set_status(f"Loaded {len(entries)} file(s) from source.")
        _xlog.info("Loaded source folder: %s  (%d files)", src, len(entries))
        self._record_path(self.source_path, self.src_combo)
        self._save_config()

    def _refresh_file_list(self):
        for w in self.rows_frame.winfo_children():
            w.destroy()
        self.check_vars.clear()

        for name, size, mtime in self.file_entries:
            var = tk.BooleanVar(value=False)
            row = ttk.Frame(self.rows_frame)
            row.pack(fill=tk.X)
            ttk.Checkbutton(row, variable=var,
                            command=self._update_selection_count).pack(side=tk.LEFT)
            ttk.Label(row, text=name,  width=44, anchor="w").pack(side=tk.LEFT)
            ttk.Label(row, text=size,  width=10, anchor="e").pack(side=tk.LEFT)
            ttk.Label(row, text=mtime, width=18, anchor="w", padding=(6, 0)).pack(side=tk.LEFT)
            self.check_vars.append((var, name))

        self._update_selection_count()

    def _update_selection_count(self):
        total    = len(self.check_vars)
        selected = sum(1 for v, _ in self.check_vars if v.get())
        self.sel_label.config(text=f"{selected} of {total} files selected")

    def select_all(self):
        for var, _ in self.check_vars:
            var.set(True)
        self._update_selection_count()

    def deselect_all(self):
        for var, _ in self.check_vars:
            var.set(False)
        self._update_selection_count()

    def copy_files(self):
        if self._busy:
            return
        src = self.source_path.get().strip()
        dst = self.dest_path.get().strip()

        if not src or not dst:
            messagebox.showwarning("Missing folders",
                                   "Please set both source and destination folders.")
            return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("Same folder",
                                 "Source and destination cannot be the same folder.")
            return
        selected = [name for var, name in self.check_vars if var.get()]
        if not selected:
            messagebox.showwarning("Nothing selected",
                                   "Please select at least one file to copy.")
            return
        if not os.path.isdir(src):
            messagebox.showerror("Source missing", f"Source folder not found:\n{src}")
            return

        if not os.path.isdir(dst):
            if not messagebox.askyesno("Create destination?",
                                       f"Destination folder does not exist:\n{dst}"
                                       "\n\nCreate it now?"):
                return
            try:
                os.makedirs(dst)
                _xlog.info("Created destination folder: %s", dst)
            except OSError as exc:
                messagebox.showerror("Cannot create folder", str(exc))
                return

        existing = [n for n in os.listdir(dst)
                    if os.path.isfile(os.path.join(dst, n)) or os.path.isdir(os.path.join(dst, n))]
        if not messagebox.askyesno("Confirm transfer",
                                   f"This will:\n\n"
                                   f"  1. DELETE all {len(existing)} item(s) currently in:\n"
                                   f"     {dst}\n\n"
                                   f"  2. COPY {len(selected)} selected file(s) from:\n"
                                   f"     {src}\n\n"
                                   f"This action cannot be undone.\n\nContinue?",
                                   icon="warning"):
            return

        self._busy = True
        self.copy_btn.state(["disabled"])
        self.set_status("Working…")
        threading.Thread(target=self._do_transfer, args=(src, dst, selected),
                         daemon=True).start()

    def _do_transfer(self, src, dst, selected):
        errors = []
        _xlog.info("=== Transfer session started ===")
        _xlog.info("Source:      %s", src)
        _xlog.info("Destination: %s", dst)

        deleted_count = 0
        try:
            for name in os.listdir(dst):
                full = os.path.join(dst, name)
                try:
                    if os.path.isfile(full) or os.path.islink(full):
                        os.remove(full)
                    elif os.path.isdir(full):
                        shutil.rmtree(full)
                    _xlog.info("DELETED: %s", name)
                    deleted_count += 1
                    self.root.after(0, self.set_status, f"Deleting: {name}")
                except OSError as exc:
                    msg = f"Could not delete {name}: {exc}"
                    _xlog.error(msg)
                    errors.append(msg)
        except OSError as exc:
            _xlog.error("Cannot list destination: %s", exc)
            errors.append(str(exc))

        copied_count = 0
        for name in selected:
            try:
                shutil.copy2(os.path.join(src, name), os.path.join(dst, name))
                size = os.path.getsize(os.path.join(dst, name))
                _xlog.info("COPIED: %s  (%s)", name, _fmt_size(size))
                copied_count += 1
                self.root.after(0, self.set_status, f"Copying: {name}")
            except OSError as exc:
                msg = f"Could not copy {name}: {exc}"
                _xlog.error(msg)
                errors.append(msg)

        _xlog.info("=== Transfer complete: %d copied, %d deleted, %d error(s) ===",
                   copied_count, deleted_count, len(errors))
        self.root.after(0, self._transfer_done, copied_count, deleted_count, errors, dst)

    def _transfer_done(self, copied, deleted, errors, dst):
        self._busy = False
        self.copy_btn.state(["!disabled"])
        self._refresh_log_display()
        self.dest_path.set(dst)
        self._record_path(self.dest_path, self.dst_combo)
        self._save_config()

        if errors:
            detail = "\n".join(errors[:10])
            if len(errors) > 10:
                detail += f"\n… and {len(errors) - 10} more (see log file)"
            messagebox.showwarning(
                "Transfer complete with errors",
                f"{copied} file(s) copied, {deleted} item(s) deleted.\n\n"
                f"Errors ({len(errors)}):\n{detail}")
            self.set_status(f"Done — {copied} copied, {deleted} deleted, "
                            f"{len(errors)} error(s).  See log.")
        else:
            messagebox.showinfo("Transfer complete",
                                f"Success!\n\n{copied} file(s) copied.\n"
                                f"{deleted} item(s) removed from destination.")
            self.set_status(f"Done — {copied} file(s) copied, {deleted} item(s) deleted.")

    # ══════════════════════════════════════════════════════════════════
    # Tab 2 — FTP logic
    # ══════════════════════════════════════════════════════════════════

    def _refresh_ftp_sites(self, silent=False):
        """Re-read FileZilla config files and rebuild the site dropdown."""
        sites, err = read_fz_sites()
        self._ftp_sites = sites

        if err:
            self.site_combo["values"] = []
            self.site_var.set("")
            self._clear_site_info()
            if not silent:
                messagebox.showwarning("FileZilla Site Manager", err)
            self.set_status("FileZilla site manager not found — see Tab 2 for details.")
            return

        names = [s["name"] for s in sites]
        self.site_combo["values"] = names

        # Restore last-used site, or fall back to the first entry
        target = self._last_ftp_site if self._last_ftp_site in names else (names[0] if names else "")
        self.site_var.set(target)
        if target:
            self._show_site_info(target)
        else:
            self._clear_site_info()

        if not silent:
            self.set_status(f"Loaded {len(sites)} FTP site(s) from FileZilla Site Manager.")

    def _on_site_selected(self, _event=None):
        name = self.site_var.get()
        self._show_site_info(name)
        self._last_ftp_site = name
        self._save_config()

    def _show_site_info(self, site_name):
        site = next((s for s in self._ftp_sites if s["name"] == site_name), None)
        if site:
            self.ftp_host_var.set(site["host"] or "—")
            self.ftp_port_var.set(str(site["port"]))
            self.ftp_user_var.set(site["user"] or "—")
            self.ftp_proto_var.set(site["protocol_str"])
            # Remote dir only updates after a real connect; preserve previous value
        else:
            self._clear_site_info()

    def _clear_site_info(self):
        for v in (self.ftp_host_var, self.ftp_port_var,
                  self.ftp_user_var, self.ftp_proto_var):
            v.set("—")
        self.ftp_remotedir_var.set("(shown after first connect)")

    def _scan_folder_b(self):
        """Count files currently in Folder B and update the display label."""
        dst = self.dest_path.get().strip()
        if not dst:
            self.ftp_filecount_var.set("— (set Folder B in Tab 1 first)")
            return
        if not os.path.isdir(dst):
            self.ftp_filecount_var.set("Folder B not found")
            return
        try:
            files = [f for f in os.listdir(dst)
                     if os.path.isfile(os.path.join(dst, f))]
            self.ftp_filecount_var.set(
                f"{len(files)} file(s) ready  ({dst})" if files
                else "0 files  — Folder B is empty"
            )
        except OSError as exc:
            self.ftp_filecount_var.set(f"Error reading folder: {exc}")

    def ftp_upload(self):
        if self._ftp_busy:
            return

        # ── Validate FTP site ──────────────────────────────────────
        site_name = self.site_var.get().strip()
        if not site_name:
            messagebox.showwarning("No site selected",
                                   "Please select an FTP site from the dropdown.")
            return
        site = next((s for s in self._ftp_sites if s["name"] == site_name), None)
        if not site:
            messagebox.showerror("Site not found",
                                 "Selected site not found. Click Refresh Sites and try again.")
            return
        if site["protocol"] != 0:
            messagebox.showerror(
                "Unsupported protocol",
                f"'{site_name}' uses {site['protocol_str']}.\n\n"
                f"This application supports plain FTP (port 21) only.\n"
                f"Please select a plain-FTP site.")
            return
        if not site["host"]:
            messagebox.showerror("No host", "The selected site has no hostname configured.")
            return

        # ── Validate Folder B ──────────────────────────────────────
        local_dir = self.dest_path.get().strip()
        if not local_dir:
            messagebox.showwarning("No Folder B",
                                   "Please set Folder B in the File Transfer tab first.")
            return
        if not os.path.isdir(local_dir):
            messagebox.showerror("Folder B missing",
                                 f"Folder B not found:\n{local_dir}")
            return
        local_files = sorted(
            f for f in os.listdir(local_dir)
            if os.path.isfile(os.path.join(local_dir, f))
        )
        if not local_files:
            messagebox.showwarning("Folder B is empty",
                                   "There are no files in Folder B to upload.")
            return

        # ── Confirmation dialog ────────────────────────────────────
        if not messagebox.askyesno(
            "Confirm FTP upload",
            f"This will connect to:\n\n"
            f"  Site:  {site['name']}\n"
            f"  Host:  {site['host']}  (port {site['port']})\n"
            f"  User:  {site['user']}\n\n"
            f"  1. DELETE all existing files in the remote directory\n"
            f"  2. UPLOAD {len(local_files)} file(s) from:\n"
            f"     {local_dir}\n\n"
            f"This action cannot be undone.\n\nContinue?",
            icon="warning",
        ):
            return

        self._ftp_busy = True
        self.ftp_btn.state(["disabled"])
        self.ftp_remotedir_var.set("Connecting…")
        self.set_status(f"Connecting to {site['host']}…")

        threading.Thread(
            target=self._do_ftp_upload,
            args=(site, local_dir, local_files),
            daemon=True,
        ).start()

    def _do_ftp_upload(self, site, local_dir, local_files):
        """Worker thread: connect → list remote → delete → upload → disconnect."""
        errors      = []
        uploaded    = 0
        ftp_deleted = 0
        remote_dir  = ""

        _flog.info("=== FTP Session started ===")
        _flog.info("FTP Site:  %s", site["name"])
        _flog.info("Host:      %s:%s", site["host"], site["port"])
        _flog.info("User:      %s", site["user"])

        ftp = ftplib.FTP()
        try:
            # ── Connect & login ────────────────────────────────────
            ftp.connect(site["host"], site["port"], timeout=30)
            _flog.info("Connected to %s", site["host"])

            ftp.login(site["user"], site["password"])
            ftp.set_pasv(True)          # passive mode — works through most firewalls

            remote_dir = ftp.pwd()
            _flog.info("Logged in. Remote directory: %s", remote_dir)
            self.root.after(0, self.ftp_remotedir_var.set, remote_dir)
            self.root.after(0, self.set_status, "Connected. Listing remote files…")

            # ── List remote files ──────────────────────────────────
            remote_names = []
            try:
                ftp.retrlines("NLST", remote_names.append)
            except ftplib.error_perm:
                pass    # empty directory — NLST returns 550

            # Keep only filenames (strip any path prefix, skip . and ..)
            remote_names = [
                os.path.basename(n) for n in remote_names
                if n not in (".", "..") and not n.endswith("/.")
            ]
            _flog.info("Remote files found: %d", len(remote_names))

            # ── Delete remote files ────────────────────────────────
            self.root.after(0, self.set_status,
                            f"Deleting {len(remote_names)} remote file(s)…")
            for name in remote_names:
                self.root.after(0, self.set_status, f"FTP deleting: {name}")
                try:
                    ftp.delete(name)
                    _flog.info("FTP DELETED: %s", name)
                    ftp_deleted += 1
                except ftplib.all_errors as exc:
                    msg = f"Could not delete remote '{name}': {exc}"
                    _flog.error(msg)
                    errors.append(msg)

            # ── Upload files ───────────────────────────────────────
            for filename in local_files:
                local_path = os.path.join(local_dir, filename)
                self.root.after(0, self.set_status, f"FTP uploading: {filename}")
                try:
                    file_size = os.path.getsize(local_path)
                    with open(local_path, "rb") as fh:
                        ftp.storbinary(f"STOR {filename}", fh)
                    _flog.info("FTP UPLOADED: %s  (%s)", filename, _fmt_size(file_size))
                    uploaded += 1
                except (ftplib.all_errors, OSError) as exc:
                    msg = f"Could not upload '{filename}': {exc}"
                    _flog.error(msg)
                    errors.append(msg)

            _flog.info(
                "=== FTP Session complete: %d uploaded, %d deleted, %d error(s) ===",
                uploaded, ftp_deleted, len(errors),
            )

        except ftplib.all_errors as exc:
            msg = f"FTP error: {exc}"
            _flog.error(msg)
            errors.append(msg)

        finally:
            # Always try to close the connection cleanly
            try:
                ftp.quit()
            except Exception:
                try:
                    ftp.close()
                except Exception:
                    pass

        self.root.after(
            0, self._ftp_done, uploaded, ftp_deleted, errors, site["name"], remote_dir
        )

    def _ftp_done(self, uploaded, ftp_deleted, errors, site_name, remote_dir):
        self._ftp_busy = False
        self.ftp_btn.state(["!disabled"])
        self.ftp_remotedir_var.set(remote_dir if remote_dir else "(connection failed)")
        self._refresh_ftp_log()

        if errors:
            detail = "\n".join(errors[:10])
            if len(errors) > 10:
                detail += f"\n… and {len(errors) - 10} more (see log file)"
            messagebox.showwarning(
                "FTP complete with errors",
                f"{uploaded} file(s) uploaded, {ftp_deleted} remote file(s) deleted.\n\n"
                f"Errors ({len(errors)}):\n{detail}")
            self.set_status(f"FTP done — {uploaded} uploaded, {ftp_deleted} deleted, "
                            f"{len(errors)} error(s).  See log.")
        else:
            messagebox.showinfo(
                "FTP Upload Complete",
                f"Success!\n\n"
                f"  Site:            {site_name}\n"
                f"  Files uploaded:  {uploaded}\n"
                f"  Remote deleted:  {ftp_deleted}")
            self.set_status(
                f"FTP done — {uploaded} file(s) uploaded, {ftp_deleted} remote file(s) deleted.")

    # ══════════════════════════════════════════════════════════════════
    # Log display helpers
    # ══════════════════════════════════════════════════════════════════

    def _refresh_log_display(self):
        self._write_log(self.log_text, self.log_lines, 12)

    def _refresh_ftp_log(self):
        self._write_log(self.ftp_log_text, self.ftp_log_lines, 15)

    @staticmethod
    def _write_log(widget, deque_ref, n):
        lines = list(deque_ref)[-n:]
        widget.configure(state=tk.NORMAL)
        widget.delete("1.0", tk.END)
        widget.insert(tk.END, "\n".join(lines))
        widget.configure(state=tk.DISABLED)
        widget.yview_moveto(1.0)

    # ══════════════════════════════════════════════════════════════════
    # Folder history & config persistence
    # ══════════════════════════════════════════════════════════════════

    def _record_path(self, var, combo):
        path = os.path.normpath(var.get().strip())
        if not path:
            return
        history = self._src_history if combo is self.src_combo else self._dst_history
        history[:] = [p for p in history if p.lower() != path.lower()]
        history.insert(0, path)
        del history[HISTORY_MAX:]
        combo["values"] = history

    def _load_config(self):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        self._src_history   = data.get("source_history", [])[:HISTORY_MAX]
        self._dst_history   = data.get("dest_history",   [])[:HISTORY_MAX]
        self._last_ftp_site = data.get("last_ftp_site",  "")

        self.src_combo["values"] = self._src_history
        self.dst_combo["values"] = self._dst_history
        if self._src_history:
            self.source_path.set(self._src_history[0])
        if self._dst_history:
            self.dest_path.set(self._dst_history[0])

    def _save_config(self):
        data = {
            "source_history": self._src_history,
            "dest_history":   self._dst_history,
            "last_ftp_site":  self._last_ftp_site,
        }
        try:
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
        except OSError as exc:
            logging.warning("Could not save config: %s", exc)

    def set_status(self, message):
        self.status_var.set(message)


def main():
    root = tk.Tk()
    style = ttk.Style(root)
    for theme in ("vista", "winnative", "clam"):
        if theme in style.theme_names():
            style.theme_use(theme)
            break
    FileTransferApp(root)
    root.mainloop()

if __name__ == "__main__":
    main()
