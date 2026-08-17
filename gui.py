"""
SEESTAR S30 PRO -- NATIVE ZWO ALPACA CONTROLLER (GUI)

Tkinter front-end over the exact same backend as main.py (the console app):
alpaca/*, diagnostics.py, imaging.py, config.py. No duplicated Alpaca logic.

All network calls run on a single background worker thread (tasks are
queued and executed one at a time, so no two Alpaca calls race each other).
Anything the backend print()s is captured and streamed into the log pane in
near-real-time. Status lights re-check every device's Connected property
after each action.

Run: python gui.py   (or double-click run_gui.bat)
"""

import os
import queue
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import numpy as np
from PIL import Image, ImageTk

import centering
import diagnostics
import imaging
import main as console
import platesolver
from alpaca import AlpacaError
from config import load_config, save_config, validate_settings

GREEN = "#2ecc71"
RED = "#e74c3c"
GRAY = "#95a5a6"
AMBER = "#f39c12"


class QueueWriter:
    """Makes print() calls inside worker-thread tasks show up in the GUI log."""

    def __init__(self, q):
        self.q = q
        self._buf = ""

    def write(self, s):
        self._buf += s
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            self.q.put(line)

    def flush(self):
        pass


class StatusLight(ttk.Frame):
    def __init__(self, parent, label):
        super().__init__(parent)
        self.dot = tk.Label(self, text="●", fg=GRAY, font=("Segoe UI", 14))
        self.dot.pack(side="left")
        ttk.Label(self, text=label).pack(side="left", padx=(2, 10))

    def set_state(self, connected):
        if connected is True:
            self.dot.configure(fg=GREEN)
        elif connected is False:
            self.dot.configure(fg=RED)
        else:
            self.dot.configure(fg=AMBER)


class App:
    def __init__(self, root):
        self.root = root
        self.root.title("Seestar S30 Pro -- Native Alpaca Controller")
        self.root.geometry("1020x860")
        self.root.minsize(760, 560)

        self.cfg = load_config()
        self.state = console.AppState(self.cfg)

        self.task_queue = queue.Queue()
        self.log_queue = queue.Queue()
        self.status_lights = {}
        self.preview_imgtk = None

        self.ui_queue = queue.Queue()

        # Camera-selection combos (Settings + Telescope tabs) share one
        # discovered-name list so both always show the same options.
        self._camera_combo_widgets = []  # [(combo_widget, its_StringVar), ...]
        self._camera_options = []
        self._camera_label_to_number = {}

        self._sc_abort_event = None  # set while a Slew & Center run is active

        self._build_ui()
        self._start_worker()
        self.root.after(80, self._pump)

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def _build_ui(self):
        top = ttk.Frame(self.root, padding=8)
        top.pack(side="top", fill="x")

        ttk.Button(top, text="Discover", command=self.on_discover).pack(side="left")
        ttk.Button(top, text="Connect All", command=self.on_connect_all).pack(side="left", padx=4)
        ttk.Button(top, text="Disconnect All", command=self.on_disconnect_all).pack(side="left")

        self.server_label = ttk.Label(top, text="No server discovered yet.")
        self.server_label.pack(side="left", padx=16)

        self.lights_frame = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        self.lights_frame.pack(side="top", fill="x")

        # Vertical paned window so the log pane can be dragged bigger/smaller
        # instead of being stuck at a fixed height.
        paned = ttk.Panedwindow(self.root, orient="vertical")
        paned.pack(side="top", fill="both", expand=True, padx=8, pady=(0, 8))

        notebook_holder = ttk.Frame(paned)
        log_frame = ttk.LabelFrame(paned, text="Status", padding=4)
        paned.add(notebook_holder, weight=3)
        paned.add(log_frame, weight=2)

        notebook = ttk.Notebook(notebook_holder)
        notebook.pack(fill="both", expand=True)

        self.tab_telescope = ttk.Frame(notebook, padding=8)
        self.tab_camera = ttk.Frame(notebook, padding=8)
        self.tab_focuser = ttk.Frame(notebook, padding=8)
        self.tab_filterwheel = ttk.Frame(notebook, padding=8)
        self.tab_switch = ttk.Frame(notebook, padding=8)
        self.tab_settings = ttk.Frame(notebook, padding=8)
        self.tab_diagnostics = ttk.Frame(notebook, padding=8)

        notebook.add(self.tab_telescope, text="Telescope")
        notebook.add(self.tab_camera, text="Camera")
        notebook.add(self.tab_focuser, text="Focuser")
        notebook.add(self.tab_filterwheel, text="Filter Wheel")
        notebook.add(self.tab_switch, text="Switch")
        notebook.add(self.tab_settings, text="Settings")
        notebook.add(self.tab_diagnostics, text="Diagnostics / Raw")

        self._build_camera_tab()
        self._build_telescope_tab()
        self._build_focuser_tab()
        self._build_filterwheel_tab()
        self._build_switch_tab()
        self._build_settings_tab()
        self._build_diagnostics_tab()

        log_toolbar = ttk.Frame(log_frame)
        log_toolbar.pack(side="top", fill="x")
        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(log_toolbar, text="Autoscroll", variable=self.autoscroll_var
                         ).pack(side="left")
        ttk.Button(log_toolbar, text="Jump to bottom",
                   command=lambda: self.log_text.see("end")).pack(side="left", padx=4)
        ttk.Button(log_toolbar, text="Clear", command=self.on_clear_log).pack(side="right")

        text_container = ttk.Frame(log_frame)
        text_container.pack(side="top", fill="both", expand=True)
        self.log_text = tk.Text(text_container, height=10, wrap="word", state="disabled",
                                 bg="#111", fg="#ddd", insertbackground="#ddd")
        scrollbar = ttk.Scrollbar(text_container, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _build_telescope_tab(self):
        f = self.tab_telescope

        conn = ttk.LabelFrame(f, text="Connection / Software Info", padding=6)
        conn.pack(fill="x", pady=4)
        ttk.Button(conn, text="Connect", command=self.on_telescope_connect).pack(side="left")
        ttk.Button(conn, text="Show Software Info",
                   command=self.on_telescope_software_info).pack(side="left", padx=4)

        state_box = ttk.LabelFrame(f, text="State", padding=6)
        state_box.pack(fill="x", pady=4)
        self.tele_state_vars = {}
        fields = ["Connected", "AtPark", "AtHome", "Slewing", "Tracking",
                  "RightAscension", "Declination", "Altitude", "Azimuth"]
        for i, name in enumerate(fields):
            r, c = divmod(i, 3)
            ttk.Label(state_box, text=name + ":").grid(row=r, column=c * 2, sticky="e", padx=4, pady=2)
            var = tk.StringVar(value="?")
            self.tele_state_vars[name] = var
            ttk.Label(state_box, textvariable=var, width=16).grid(row=r, column=c * 2 + 1, sticky="w")
        ttk.Button(state_box, text="Refresh State",
                   command=self.on_telescope_refresh_state).grid(row=3, column=0, columnspan=2, pady=4, sticky="w")

        init_box = ttk.LabelFrame(f, text="Initialize / Park", padding=6)
        init_box.pack(fill="x", pady=4)
        ttk.Button(init_box, text="Initialize / Open Arm (Unpark+FindHome)",
                   command=self.on_telescope_initialize).pack(side="left")
        ttk.Button(init_box, text="Park", command=self.on_telescope_park).pack(side="left", padx=4)
        ttk.Button(init_box, text="Abort Slew", command=self.on_telescope_abort).pack(side="left")

        track_box = ttk.LabelFrame(f, text="Tracking", padding=6)
        track_box.pack(fill="x", pady=4)
        self.tracking_var = tk.StringVar(value="on")
        ttk.Radiobutton(track_box, text="On", variable=self.tracking_var, value="on").pack(side="left")
        ttk.Radiobutton(track_box, text="Off", variable=self.tracking_var, value="off").pack(side="left")
        ttk.Button(track_box, text="Set Tracking",
                   command=self.on_telescope_set_tracking).pack(side="left", padx=8)

        slew_box = ttk.LabelFrame(f, text="Slew (Auto Start / First Slew Test)", padding=6)
        slew_box.pack(fill="x", pady=4)
        ttk.Label(slew_box, text="Target:").grid(row=0, column=0, sticky="e")
        self.target_var = tk.StringVar(value="(manual RA/Dec)")
        targets = ["(manual RA/Dec)"] + sorted(console.CATALOG.keys())
        target_combo = ttk.Combobox(slew_box, textvariable=self.target_var, values=targets,
                                     state="readonly", width=18)
        target_combo.grid(row=0, column=1, padx=4)
        target_combo.bind("<<ComboboxSelected>>", self._on_target_selected)

        ttk.Label(slew_box, text="RA (hours):").grid(row=1, column=0, sticky="e")
        self.ra_var = tk.StringVar()
        ttk.Entry(slew_box, textvariable=self.ra_var, width=12).grid(row=1, column=1, sticky="w")
        ttk.Label(slew_box, text="Dec (deg):").grid(row=1, column=2, sticky="e")
        self.dec_var = tk.StringVar()
        ttk.Entry(slew_box, textvariable=self.dec_var, width=12).grid(row=1, column=3, sticky="w")
        ttk.Button(slew_box, text="Run First Slew Test",
                   command=self.on_telescope_slew).grid(row=1, column=4, padx=8)

        axis_box = ttk.LabelFrame(f, text="Manual Axis Movement (MoveAxis)", padding=6)
        axis_box.pack(fill="x", pady=4)
        ttk.Label(axis_box, text="Axis:").grid(row=0, column=0, sticky="e")
        self.axis_var = tk.StringVar(value="0")
        ttk.Combobox(axis_box, textvariable=self.axis_var, values=["0", "1"],
                     state="readonly", width=4).grid(row=0, column=1, sticky="w")
        ttk.Label(axis_box, text="Rate:").grid(row=0, column=2, sticky="e")
        self.rate_var = tk.StringVar(value="1.0")
        ttk.Entry(axis_box, textvariable=self.rate_var, width=8).grid(row=0, column=3, sticky="w")
        ttk.Button(axis_box, text="Check AxisRates",
                   command=self.on_axis_rates).grid(row=0, column=4, padx=4)
        ttk.Button(axis_box, text="Pulse 0.5s",
                   command=self.on_axis_pulse).grid(row=0, column=5, padx=4)

        sc_box = ttk.LabelFrame(f, text="Slew & Center (plate-solve loop)", padding=6)
        sc_box.pack(fill="x", pady=4)
        ttk.Label(sc_box, foreground="#888",
                  text="Uses the Target/RA/Dec fields above. Defaults come from Settings; "
                       "changes here are a one-off override for this run only."
                  ).grid(row=0, column=0, columnspan=4, sticky="w")

        ttk.Label(sc_box, text="Camera:").grid(row=1, column=0, sticky="e")
        self.sc_camera_var = tk.StringVar()
        self.sc_camera_combo = ttk.Combobox(sc_box, textvariable=self.sc_camera_var,
                                             state="readonly", width=32)
        self.sc_camera_combo.grid(row=1, column=1, sticky="w", padx=4)
        self._camera_combo_widgets.append((self.sc_camera_combo, self.sc_camera_var))

        ttk.Label(sc_box, text="Exposure (s):").grid(row=1, column=2, sticky="e")
        self.sc_exposure_var = tk.StringVar()
        ttk.Entry(sc_box, textvariable=self.sc_exposure_var, width=8).grid(
            row=1, column=3, sticky="w", padx=4)

        ttk.Label(sc_box, text="Tolerance (arcmin):").grid(row=2, column=0, sticky="e")
        self.sc_tolerance_var = tk.StringVar()
        ttk.Entry(sc_box, textvariable=self.sc_tolerance_var, width=8).grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Label(sc_box, text="Iterations:").grid(row=2, column=2, sticky="e")
        self.sc_iterations_var = tk.StringVar()
        ttk.Entry(sc_box, textvariable=self.sc_iterations_var, width=8).grid(
            row=2, column=3, sticky="w", padx=4)

        sc_btn_row = ttk.Frame(sc_box)
        sc_btn_row.grid(row=3, column=0, columnspan=4, pady=6, sticky="w")
        ttk.Button(sc_btn_row, text="Slew & Center", command=self.on_slew_and_center).pack(side="left")
        ttk.Button(sc_btn_row, text="Abort", command=self.on_slew_and_center_abort).pack(
            side="left", padx=4)

        progress_box = ttk.LabelFrame(f, text="Slew & Center Progress", padding=6)
        progress_box.pack(fill="x", pady=4)
        self.sc_progress_vars = {}
        for i, label in enumerate(["Iteration", "Target", "Solved", "Pointing error", "Status"]):
            ttk.Label(progress_box, text=label + ":").grid(row=i, column=0, sticky="e", pady=1)
            var = tk.StringVar(value="-")
            self.sc_progress_vars[label] = var
            ttk.Label(progress_box, textvariable=var).grid(row=i, column=1, sticky="w", padx=4)

    def _on_target_selected(self, _event):
        name = self.target_var.get()
        if name in console.CATALOG:
            ra, dec = console.CATALOG[name]
            self.ra_var.set(str(ra))
            self.dec_var.set(str(dec))

    def _build_camera_tab(self):
        f = self.tab_camera
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Camera:").pack(side="left")
        self.camera_var = tk.StringVar(value="0")
        ttk.Combobox(top, textvariable=self.camera_var, values=["0", "1"],
                     state="readonly", width=4).pack(side="left", padx=4)
        ttk.Button(top, text="Connect", command=self.on_camera_connect).pack(side="left", padx=4)
        ttk.Button(top, text="List Cameras", command=self.on_camera_list).pack(side="left")

        exp_box = ttk.LabelFrame(f, text="Exposure", padding=6)
        exp_box.pack(fill="x", pady=4)
        ttk.Label(exp_box, text="Duration (s):").grid(row=0, column=0, sticky="e")
        self.exposure_var = tk.StringVar(value="2.0")
        ttk.Entry(exp_box, textvariable=self.exposure_var, width=10).grid(row=0, column=1, sticky="w")
        self.light_var = tk.StringVar(value="light")
        ttk.Radiobutton(exp_box, text="Light", variable=self.light_var, value="light").grid(row=0, column=2)
        ttk.Radiobutton(exp_box, text="Dark", variable=self.light_var, value="dark").grid(row=0, column=3)
        ttk.Button(exp_box, text="Take Exposure",
                   command=self.on_camera_expose).grid(row=0, column=4, padx=8)

        save_box = ttk.LabelFrame(f, text="Save to this laptop", padding=6)
        save_box.pack(fill="x", pady=4)
        ttk.Label(save_box, text="Output folder:").grid(row=0, column=0, sticky="e")
        default_dir = os.path.join(console.SCRIPT_DIR, self.cfg.get("output_directory", "images"))
        self.output_dir_var = tk.StringVar(value=os.path.abspath(default_dir))
        ttk.Entry(save_box, textvariable=self.output_dir_var, width=55).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(save_box, text="Browse...", command=self.on_browse_output_dir).grid(row=0, column=2)
        save_box.columnconfigure(1, weight=1)
        ttk.Button(save_box, text="Save FITS + PNG",
                   command=self.on_camera_save).grid(row=1, column=0, pady=6, sticky="w")
        self.last_saved_var = tk.StringVar(value="(nothing saved yet)")
        ttk.Label(save_box, textvariable=self.last_saved_var, foreground="#2a7").grid(
            row=1, column=1, columnspan=2, sticky="w")

        self.preview_label = ttk.Label(f, text="(no preview yet)")
        self.preview_label.pack(pady=8)

    def _build_focuser_tab(self):
        f = self.tab_focuser
        top = ttk.Frame(f)
        top.pack(fill="x", pady=4)
        ttk.Label(top, text="Focuser:").pack(side="left")
        self.focuser_var = tk.StringVar(value="0")
        ttk.Combobox(top, textvariable=self.focuser_var, values=["0", "1"],
                     state="readonly", width=4).pack(side="left", padx=4)
        ttk.Button(top, text="Connect", command=self.on_focuser_connect).pack(side="left", padx=4)
        ttk.Button(top, text="Show Focusers", command=self.on_focuser_show).pack(side="left")

        move_box = ttk.LabelFrame(f, text="Move", padding=6)
        move_box.pack(fill="x", pady=4)
        ttk.Label(move_box, text="Absolute position:").pack(side="left")
        self.focuser_pos_var = tk.StringVar()
        ttk.Entry(move_box, textvariable=self.focuser_pos_var, width=10).pack(side="left", padx=4)
        ttk.Button(move_box, text="Move", command=self.on_focuser_move).pack(side="left", padx=4)
        ttk.Button(move_box, text="Halt", command=self.on_focuser_halt).pack(side="left")

    def _build_filterwheel_tab(self):
        f = self.tab_filterwheel
        ttk.Button(f, text="Connect + Refresh", command=self.on_filterwheel_show).pack(anchor="w", pady=4)
        self.filter_listbox = tk.Listbox(f, height=8, exportselection=False)
        self.filter_listbox.pack(fill="x", pady=4)
        ttk.Button(f, text="Select Highlighted Filter",
                   command=self.on_filterwheel_select).pack(anchor="w")

    def _build_switch_tab(self):
        f = self.tab_switch
        ttk.Button(f, text="Connect + Refresh", command=self.on_switch_show).pack(anchor="w", pady=4)
        self.switch_rows_frame = ttk.Frame(f)
        self.switch_rows_frame.pack(fill="x", pady=4)
        self.switch_widgets = {}

    def _build_settings_tab(self):
        f = self.tab_settings

        conn_box = ttk.LabelFrame(f, text="Connection", padding=6)
        conn_box.pack(fill="x", pady=4)
        ttk.Label(conn_box, text="Client ID:").grid(row=0, column=0, sticky="e")
        self.set_client_id_var = tk.StringVar()
        ttk.Entry(conn_box, textvariable=self.set_client_id_var, width=10).grid(
            row=0, column=1, sticky="w", padx=4)
        ttk.Label(conn_box, text="Discovery timeout (s):").grid(row=0, column=2, sticky="e")
        self.set_discovery_timeout_var = tk.StringVar()
        ttk.Entry(conn_box, textvariable=self.set_discovery_timeout_var, width=8).grid(
            row=0, column=3, sticky="w", padx=4)

        ttk.Label(conn_box, text="Preferred server IP:").grid(row=1, column=0, sticky="e")
        self.set_pref_ip_var = tk.StringVar()
        ttk.Entry(conn_box, textvariable=self.set_pref_ip_var, width=16).grid(
            row=1, column=1, sticky="w", padx=4)
        ttk.Label(conn_box, text="Preferred server port:").grid(row=1, column=2, sticky="e")
        self.set_pref_port_var = tk.StringVar()
        ttk.Entry(conn_box, textvariable=self.set_pref_port_var, width=8).grid(
            row=1, column=3, sticky="w", padx=4)
        ttk.Label(conn_box, foreground="#888", wraplength=560, justify="left",
                  text="Preferred server IP/port are only used as a fallback when Alpaca UDP "
                       "discovery finds nothing -- normal operation always uses discovery, and "
                       "the Alpaca port always comes from there, never from this setting."
                  ).grid(row=2, column=0, columnspan=4, sticky="w", pady=(4, 0))

        out_box = ttk.LabelFrame(f, text="Output", padding=6)
        out_box.pack(fill="x", pady=4)
        ttk.Label(out_box, text="Image/output folder:").grid(row=0, column=0, sticky="e")
        ttk.Entry(out_box, textvariable=self.output_dir_var, width=55).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(out_box, text="Browse...", command=self.on_browse_output_dir).grid(row=0, column=2)
        out_box.columnconfigure(1, weight=1)

        solve_box = ttk.LabelFrame(f, text="Plate Solving", padding=6)
        solve_box.pack(fill="x", pady=4)
        ttk.Label(solve_box, text="ASTAP executable:").grid(row=0, column=0, sticky="e")
        self.astap_path_var = tk.StringVar()
        ttk.Entry(solve_box, textvariable=self.astap_path_var, width=55).grid(
            row=0, column=1, sticky="we", padx=4)
        ttk.Button(solve_box, text="Browse...", command=self.on_astap_browse).grid(row=0, column=2)
        solve_box.columnconfigure(1, weight=1)

        self.astap_status_var = tk.StringVar(value="(not checked yet)")
        ttk.Label(solve_box, textvariable=self.astap_status_var).grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(4, 0))

        solve_btn_row = ttk.Frame(solve_box)
        solve_btn_row.grid(row=2, column=0, columnspan=3, sticky="w", pady=4)
        ttk.Button(solve_btn_row, text="Detect Automatically",
                   command=self.on_astap_detect).pack(side="left")
        ttk.Button(solve_btn_row, text="Test ASTAP", command=self.on_astap_test).pack(
            side="left", padx=4)

        ttk.Label(solve_box, text="Plate solve timeout (s):").grid(row=3, column=0, sticky="e")
        self.plate_solve_timeout_var = tk.StringVar()
        ttk.Entry(solve_box, textvariable=self.plate_solve_timeout_var, width=8).grid(
            row=3, column=1, sticky="w", padx=4)

        center_box = ttk.LabelFrame(f, text="Slew & Center defaults", padding=6)
        center_box.pack(fill="x", pady=4)
        ttk.Label(center_box, text="Centering camera:").grid(row=0, column=0, sticky="e")
        self.centering_camera_var = tk.StringVar()
        self.centering_camera_combo = ttk.Combobox(
            center_box, textvariable=self.centering_camera_var, state="readonly", width=34)
        self.centering_camera_combo.grid(row=0, column=1, sticky="w", padx=4)
        self._camera_combo_widgets.append((self.centering_camera_combo, self.centering_camera_var))

        ttk.Label(center_box, text="Exposure (s):").grid(row=0, column=2, sticky="e")
        self.centering_exposure_var = tk.StringVar()
        ttk.Entry(center_box, textvariable=self.centering_exposure_var, width=8).grid(
            row=0, column=3, sticky="w", padx=4)

        ttk.Label(center_box, text="Tolerance (arcmin):").grid(row=1, column=0, sticky="e")
        self.centering_tolerance_var = tk.StringVar()
        ttk.Entry(center_box, textvariable=self.centering_tolerance_var, width=8).grid(
            row=1, column=1, sticky="w", padx=4)
        ttk.Label(center_box, text="Max iterations:").grid(row=1, column=2, sticky="e")
        self.centering_iterations_var = tk.StringVar()
        ttk.Entry(center_box, textvariable=self.centering_iterations_var, width=8).grid(
            row=1, column=3, sticky="w", padx=4)

        ttk.Label(center_box, text="Minimum altitude (deg):").grid(row=2, column=0, sticky="e")
        self.min_altitude_var = tk.StringVar()
        ttk.Entry(center_box, textvariable=self.min_altitude_var, width=8).grid(
            row=2, column=1, sticky="w", padx=4)
        ttk.Label(center_box, text="Sun exclusion radius (deg):").grid(row=2, column=2, sticky="e")
        self.sun_exclusion_var = tk.StringVar()
        ttk.Entry(center_box, textvariable=self.sun_exclusion_var, width=8).grid(
            row=2, column=3, sticky="w", padx=4)

        save_row = ttk.Frame(f)
        save_row.pack(fill="x", pady=8)
        ttk.Button(save_row, text="Save Settings", command=self.on_settings_save).pack(side="left")
        ttk.Button(save_row, text="Reload", command=self.on_settings_reload).pack(side="left", padx=4)
        self.settings_status_var = tk.StringVar(value="")
        ttk.Label(save_row, textvariable=self.settings_status_var).pack(side="left", padx=8)

        self._populate_settings_widgets()

    def _build_diagnostics_tab(self):
        f = self.tab_diagnostics

        report_box = ttk.LabelFrame(f, text="Reports", padding=6)
        report_box.pack(fill="x", pady=4)
        ttk.Button(report_box, text="Show All Capabilities",
                   command=self.on_show_capabilities).pack(side="left")
        ttk.Button(report_box, text="Show SupportedActions",
                   command=self.on_show_supported_actions).pack(side="left", padx=4)
        ttk.Button(report_box, text="Dump Device Info",
                   command=self.on_dump_device_info).pack(side="left")
        ttk.Button(report_box, text="Show Arm/Movement Summary",
                   command=self.on_show_summary).pack(side="left", padx=4)

        raw_box = ttk.LabelFrame(f, text="Raw Alpaca GET / PUT", padding=6)
        raw_box.pack(fill="x", pady=4)
        ttk.Label(raw_box, text="Device type:").grid(row=0, column=0, sticky="e")
        self.raw_type_var = tk.StringVar(value="telescope")
        ttk.Combobox(raw_box, textvariable=self.raw_type_var,
                     values=["telescope", "camera", "focuser", "filterwheel", "switch"],
                     state="readonly", width=12).grid(row=0, column=1, sticky="w")
        ttk.Label(raw_box, text="Number:").grid(row=0, column=2, sticky="e")
        self.raw_num_var = tk.StringVar(value="0")
        ttk.Entry(raw_box, textvariable=self.raw_num_var, width=4).grid(row=0, column=3, sticky="w")
        ttk.Label(raw_box, text="Member:").grid(row=1, column=0, sticky="e")
        self.raw_member_var = tk.StringVar(value="connected")
        ttk.Entry(raw_box, textvariable=self.raw_member_var, width=20).grid(row=1, column=1, columnspan=2, sticky="w")
        ttk.Label(raw_box, text="Params (k=v,k2=v2):").grid(row=2, column=0, sticky="e")
        self.raw_params_var = tk.StringVar()
        ttk.Entry(raw_box, textvariable=self.raw_params_var, width=30).grid(row=2, column=1, columnspan=3, sticky="w")
        ttk.Button(raw_box, text="Send GET", command=self.on_raw_get).grid(row=3, column=0, pady=4)
        ttk.Button(raw_box, text="Send PUT", command=self.on_raw_put).grid(row=3, column=1, pady=4)

    # ------------------------------------------------------------------
    # Worker thread / logging plumbing
    # ------------------------------------------------------------------

    def _start_worker(self):
        t = threading.Thread(target=self._worker_loop, daemon=True)
        t.start()

    def _worker_loop(self):
        while True:
            fn = self.task_queue.get()
            old_stdout = sys.stdout
            sys.stdout = QueueWriter(self.log_queue)
            try:
                fn()
            except Exception as e:
                print(f"UNEXPECTED ERROR: {e}")
            finally:
                sys.stdout = old_stdout
            if fn is not self._refresh_status_lights_task:
                # Refresh status lights after every REAL action, but never
                # chain a refresh after a refresh -- that would spin forever.
                self.log_queue.put("")
                if self.state.devices:
                    self.task_queue.put(self._refresh_status_lights_task)

    def _pump(self):
        """Runs only on the main thread (self-rescheduled via after()). Safe
        place to touch Tk widgets -- drains both the log queue and the
        generic ui-callable queue that worker-thread code posts to via
        self.ui(fn). Worker threads must NEVER call Tk/after() directly."""
        lines = []
        try:
            while True:
                lines.append(self.log_queue.get_nowait())
        except queue.Empty:
            pass
        if lines:
            self.log_text.configure(state="normal")
            for line in lines:
                self.log_text.insert("end", line + "\n")
            if self.autoscroll_var.get():
                self.log_text.see("end")
            self.log_text.configure(state="disabled")

        try:
            while True:
                fn = self.ui_queue.get_nowait()
                fn()
        except queue.Empty:
            pass

        self.root.after(80, self._pump)

    def ui(self, fn):
        """Call from a worker thread to run fn() on the main thread later."""
        self.ui_queue.put(fn)

    def submit(self, fn):
        self.task_queue.put(fn)

    def on_clear_log(self):
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    # ------------------------------------------------------------------
    # Status lights
    # ------------------------------------------------------------------

    def _refresh_status_lights_task(self):
        results = {}
        for key, dev in list(self.state.devices.items()):
            try:
                results[key] = dev.connected
            except Exception:
                results[key] = None
        self.ui(lambda: self._apply_status_lights(results))

    def _apply_status_lights(self, results):
        for key, light in self.status_lights.items():
            if key in results:
                light.set_state(results[key])

    def _rebuild_status_lights(self):
        for child in self.lights_frame.winfo_children():
            child.destroy()
        self.status_lights = {}
        for key in sorted(self.state.devices.keys()):
            dtype, dnum = key
            light = StatusLight(self.lights_frame, f"{dtype.capitalize()} {dnum}")
            light.pack(side="left")
            self.status_lights[key] = light

    def _update_camera_options(self):
        """Refreshes every registered camera combo (Settings + Telescope
        tabs) with real discovered camera names, e.g.
        'Camera 1 -- Seestar S30 Pro Wide Angle Camera' instead of a bare
        number. Falls back to just the configured number if nothing has
        been discovered yet."""
        cams = sorted(self.state.by_type("camera"), key=lambda c: c.device_number)
        if cams:
            self._camera_options = [f"Camera {c.device_number} -- {c.display_name()}" for c in cams]
            self._camera_label_to_number = {
                label: c.device_number for label, c in zip(self._camera_options, cams)
            }
        else:
            n = self.cfg.get("centering_camera", 0)
            self._camera_options = [f"Camera {n}"]
            self._camera_label_to_number = {self._camera_options[0]: n}

        wanted = self.cfg.get("centering_camera", 0)
        preferred_label = next(
            (lbl for lbl, num in self._camera_label_to_number.items() if num == wanted),
            self._camera_options[0])

        for combo, var in self._camera_combo_widgets:
            combo["values"] = self._camera_options
            if var.get() not in self._camera_options:
                var.set(preferred_label)

    def _parse_camera_number(self, label):
        return self._camera_label_to_number.get(label, self.cfg.get("centering_camera", 0))

    # ------------------------------------------------------------------
    # Top bar handlers
    # ------------------------------------------------------------------

    def on_discover(self):
        def task():
            console.do_discover(self.state)
            self.ui(self._after_discover)
        self.submit(task)

    def _after_discover(self):
        self._rebuild_status_lights()
        self._update_camera_options()
        if self.state.server:
            self.server_label.configure(
                text=f"Server: {self.state.server.ip}:{self.state.server.port}  "
                     f"({len(self.state.devices)} device(s))")
        else:
            self.server_label.configure(text="No server discovered.")

    def on_connect_all(self):
        def task():
            for dev in self.state.devices.values():
                diagnostics.report_action(dev, "Connect", lambda d=dev: setattr(d, "connected", True))
        self.submit(task)

    def on_disconnect_all(self):
        def task():
            for dev in self.state.devices.values():
                diagnostics.report_action(dev, "Disconnect", lambda d=dev: setattr(d, "connected", False))
        self.submit(task)

    # ------------------------------------------------------------------
    # Telescope handlers
    # ------------------------------------------------------------------

    def _telescope(self):
        return self.state.devices.get(("telescope", 0)) or next(
            (d for (t, _), d in self.state.devices.items() if t == "telescope"), None)

    def on_telescope_connect(self):
        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            diagnostics.report_action(t, "Connect", lambda: setattr(t, "connected", True))
        self.submit(task)

    def on_telescope_software_info(self):
        def task():
            t = self._telescope()
            if not t or not self.state.server:
                print("Need a discovered server + telescope first.")
                return
            self.state.software_info = diagnostics.print_software_info(self.state.server, t)
        self.submit(task)

    def on_telescope_refresh_state(self):
        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            values = {}
            for label, prop in [("Connected", "connected"), ("AtPark", "atpark"),
                                 ("AtHome", "athome"), ("Slewing", "slewing"),
                                 ("Tracking", "tracking"), ("RightAscension", "rightascension"),
                                 ("Declination", "declination"), ("Altitude", "altitude"),
                                 ("Azimuth", "azimuth")]:
                try:
                    values[label] = t.get(prop) if prop != "connected" else t.connected
                except AlpacaError as e:
                    values[label] = f"ERR({e.error_number})"
            print("Telescope state refreshed.")
            self.ui(lambda: self._apply_telescope_state(values))
        self.submit(task)

    def _apply_telescope_state(self, values):
        for k, v in values.items():
            if k in self.tele_state_vars:
                self.tele_state_vars[k].set(str(v))

    def on_telescope_initialize(self):
        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            diagnostics.initialize_telescope(t)
        self.submit(task)

    def on_telescope_park(self):
        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            if not diagnostics.report_action(t, "CanPark", lambda: t.canpark):
                print("Device does not report CanPark=True -- not sending Park.")
                return
            diagnostics.report_action(t, "Park", t.park)
        self.submit(task)

    def on_telescope_abort(self):
        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            diagnostics.report_action(t, "AbortSlew", t.abort_slew)
        self.submit(task)

    def on_telescope_set_tracking(self):
        want_on = self.tracking_var.get() == "on"

        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            if not diagnostics.report_action(t, "CanSetTracking", lambda: t.cansettracking):
                print("Device does not report CanSetTracking=True.")
                return
            diagnostics.report_action(t, "Set Tracking", lambda: setattr(t, "tracking", want_on))
        self.submit(task)

    def on_telescope_slew(self):
        ra_raw, dec_raw = self.ra_var.get().strip(), self.dec_var.get().strip()

        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            ra = dec = None
            if ra_raw and dec_raw:
                try:
                    ra, dec = float(ra_raw), float(dec_raw)
                except ValueError:
                    print("Invalid RA/Dec -- aborting.")
                    return
            else:
                print("No RA/Dec given -- using current RA + 1h (see log below).")
            self.state.slew_test_results = diagnostics.run_first_slew_test(t, ra=ra, dec=dec)
        self.submit(task)

    def on_axis_rates(self):
        axis = int(self.axis_var.get())

        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            can = diagnostics.report_action(t, f"CanMoveAxis({axis})", lambda: t.can_move_axis(axis))
            if can:
                diagnostics.report_action(t, f"AxisRates({axis})", lambda: t.axis_rates(axis))
        self.submit(task)

    def on_axis_pulse(self):
        axis = int(self.axis_var.get())
        try:
            rate = float(self.rate_var.get())
        except ValueError:
            self.submit(lambda: print("Invalid rate."))
            return

        def task():
            t = self._telescope()
            if not t:
                print("No telescope known -- run Discover first.")
                return
            before = diagnostics._telescope_snapshot(t)
            print(f"MoveAxis(Axis={axis}, Rate={rate}) for 0.5s ...")
            diagnostics.try_action(t, "MoveAxis start", lambda: t.move_axis(axis, rate))
            time.sleep(0.5)
            diagnostics.try_action(t, "MoveAxis stop", lambda: t.move_axis(axis, 0))
            after = diagnostics._telescope_snapshot(t)
            moved = diagnostics._coords_changed(before, after, epsilon=0.02)
            print(f"Before: {before}")
            print(f"After:  {after}")
            print(f"Reported coordinate change: {moved} (self-reported -- confirm visually)")
        self.submit(task)

    # ------------------------------------------------------------------
    # Camera handlers
    # ------------------------------------------------------------------

    def _camera(self):
        num = int(self.camera_var.get())
        return self.state.devices.get(("camera", num))

    def on_camera_list(self):
        self.submit(lambda: console.camera_list(self.state))

    def on_camera_connect(self):
        def task():
            c = self._camera()
            if not c:
                print("No such camera known -- run Discover first.")
                return
            diagnostics.report_action(c, "Connect", lambda: setattr(c, "connected", True))
        self.submit(task)

    def on_camera_expose(self):
        try:
            duration = float(self.exposure_var.get())
        except ValueError:
            self.submit(lambda: print("Invalid exposure duration."))
            return
        light = self.light_var.get() == "light"

        def task():
            c = self._camera()
            if not c:
                print("No such camera known -- run Discover first.")
                return
            if not c.connected:
                print("Camera is not connected.")
                return
            try:
                sensor_type = c.sensortype
            except AlpacaError:
                sensor_type = None
            print(f"Starting {duration}s exposure on {c.label()} ...")
            try:
                array = imaging.take_exposure(c, duration, light=light)
            except AlpacaError as e:
                print(f"[{c.label()}] StartExposure FAILED")
                print(f"  Alpaca ErrorNumber: {e.error_number}")
                print(f"  ErrorMessage: {e.error_message}")
                return
            except imaging.ExposureTimeout as e:
                print(f"[{c.label()}] {e}")
                return
            arr = np.asarray(array)
            print(f"[{c.label()}] Exposure OK -- shape {arr.shape}, dtype {arr.dtype}, "
                  f"min={arr.min()} max={arr.max()}")
            self.state.last_exposure = (c, arr, sensor_type)
            print("Use 'Save FITS + PNG' to write it to disk.")
        self.submit(task)

    def on_browse_output_dir(self):
        chosen = filedialog.askdirectory(
            initialdir=self.output_dir_var.get() or console.SCRIPT_DIR,
            title="Choose where to save FITS/PNG files")
        if chosen:
            self.output_dir_var.set(chosen)

    def on_camera_save(self):
        chosen_dir = self.output_dir_var.get().strip()

        def task():
            if chosen_dir:
                self.state.cfg["output_directory"] = chosen_dir
                try:
                    save_config(self.state.cfg)
                except OSError as e:
                    print(f"Could not persist output_directory to config.json: {e}")
            paths = console.camera_save_last(self.state)
            if paths:
                self.ui(lambda: self._on_saved(paths))
        self.submit(task)

    def _on_saved(self, paths):
        summary = f"Saved: {paths['fits']}"
        if paths.get("png"):
            summary += f"   (+ preview PNG)"
        self.last_saved_var.set(summary)
        if paths.get("png"):
            try:
                img = Image.open(paths["png"])
                img.thumbnail((420, 420))
                self.preview_imgtk = ImageTk.PhotoImage(img)
                self.preview_label.configure(image=self.preview_imgtk, text="")
            except Exception as e:
                self.preview_label.configure(text=f"(preview failed: {e})")

    # ------------------------------------------------------------------
    # Focuser handlers
    # ------------------------------------------------------------------

    def _focuser(self):
        num = int(self.focuser_var.get())
        return self.state.devices.get(("focuser", num))

    def on_focuser_show(self):
        self.submit(lambda: console.focuser_show(self.state))

    def on_focuser_connect(self):
        def task():
            fo = self._focuser()
            if not fo:
                print("No such focuser known -- run Discover first.")
                return
            diagnostics.report_action(fo, "Connect", lambda: setattr(fo, "connected", True))
        self.submit(task)

    def on_focuser_move(self):
        try:
            pos = int(self.focuser_pos_var.get())
        except ValueError:
            self.submit(lambda: print("Invalid position."))
            return

        def task():
            fo = self._focuser()
            if not fo:
                print("No such focuser known -- run Discover first.")
                return
            diagnostics.report_action(fo, "Move", lambda: fo.move(pos))
        self.submit(task)

    def on_focuser_halt(self):
        def task():
            fo = self._focuser()
            if not fo:
                print("No such focuser known -- run Discover first.")
                return
            diagnostics.report_action(fo, "Halt", fo.halt)
        self.submit(task)

    # ------------------------------------------------------------------
    # Filter wheel handlers
    # ------------------------------------------------------------------

    def _filterwheel(self):
        return self.state.devices.get(("filterwheel", 0))

    def on_filterwheel_show(self):
        def task():
            fw = self._filterwheel()
            if not fw:
                print("No filter wheel known -- run Discover first.")
                return
            if not fw.connected:
                diagnostics.report_action(fw, "Connect", lambda: setattr(fw, "connected", True))
            try:
                names = fw.names
                pos = fw.position
            except AlpacaError as e:
                print(f"Could not read filter wheel: {e}")
                return
            print(f"Filters: {names}  (current position {pos})")
            self.ui(lambda: self._fill_filter_listbox(names, pos))
        self.submit(task)

    def _fill_filter_listbox(self, names, pos):
        self.filter_listbox.delete(0, "end")
        for i, name in enumerate(names):
            marker = "  <-- current" if i == pos else ""
            self.filter_listbox.insert("end", f"{i}  {name}{marker}")

    def on_filterwheel_select(self):
        sel = self.filter_listbox.curselection()
        if not sel:
            self.submit(lambda: print("No filter highlighted."))
            return
        idx = sel[0]

        def task():
            fw = self._filterwheel()
            if not fw:
                print("No filter wheel known -- run Discover first.")
                return
            diagnostics.report_action(fw, "SetPosition", lambda: setattr(fw, "position", idx))
        self.submit(task)

    # ------------------------------------------------------------------
    # Switch handlers
    # ------------------------------------------------------------------

    def _switch(self):
        return self.state.devices.get(("switch", 0))

    def on_switch_show(self):
        def task():
            sw = self._switch()
            if not sw:
                print("No switch device known -- run Discover first.")
                return
            if not sw.connected:
                diagnostics.report_action(sw, "Connect", lambda: setattr(sw, "connected", True))
            try:
                n = sw.maxswitch
            except AlpacaError as e:
                print(f"Could not read MaxSwitch: {e}")
                return
            rows = []
            for i in range(n or 0):
                try:
                    rows.append({
                        "id": i,
                        "name": sw.get_switch_name(i),
                        "value": sw.get_switch_value(i),
                        "can_write": sw.can_write(i),
                    })
                except AlpacaError as e:
                    print(f"switch[{i}]: ERROR ({e.error_number}) {e.error_message}")
            print(f"Read {len(rows)} switch(es).")
            self.ui(lambda: self._rebuild_switch_rows(rows))
        self.submit(task)

    def _rebuild_switch_rows(self, rows):
        for child in self.switch_rows_frame.winfo_children():
            child.destroy()
        for row in rows:
            r = ttk.Frame(self.switch_rows_frame)
            r.pack(fill="x", pady=2)
            ttk.Label(r, text=f"[{row['id']}] {row['name']}", width=30).pack(side="left")
            ttk.Label(r, text=f"value={row['value']}", width=14).pack(side="left")
            if row["can_write"]:
                ttk.Button(r, text="Turn ON", command=lambda i=row["id"]: self.on_switch_set(i, True)
                           ).pack(side="left", padx=2)
                ttk.Button(r, text="Turn OFF", command=lambda i=row["id"]: self.on_switch_set(i, False)
                           ).pack(side="left")
            else:
                ttk.Label(r, text="(read-only)").pack(side="left")

    def on_switch_set(self, idx, on):
        def task():
            sw = self._switch()
            if not sw:
                print("No switch device known -- run Discover first.")
                return
            diagnostics.report_action(sw, "SetSwitch", lambda: sw.set_switch(idx, on))
        self.submit(task)

    # ------------------------------------------------------------------
    # Settings handlers
    # ------------------------------------------------------------------

    def _populate_settings_widgets(self):
        c = self.cfg
        self.set_client_id_var.set(str(c.get("client_id", 1234)))
        self.set_discovery_timeout_var.set(str(c.get("discovery_timeout", 3)))
        self.set_pref_ip_var.set(c.get("preferred_server_ip") or "")
        self.set_pref_port_var.set(
            str(c.get("preferred_server_port")) if c.get("preferred_server_port") else "")
        self.astap_path_var.set(c.get("astap_path", ""))
        self.plate_solve_timeout_var.set(str(c.get("plate_solve_timeout", 60)))
        self.centering_tolerance_var.set(str(c.get("centering_tolerance_arcmin", 5.0)))
        self.centering_iterations_var.set(str(c.get("centering_max_iterations", 3)))
        self.centering_exposure_var.set(str(c.get("centering_exposure_seconds", 2.0)))
        self.min_altitude_var.set(str(c.get("minimum_target_altitude_deg", 20.0)))
        self.sun_exclusion_var.set(str(c.get("sun_exclusion_deg", 30.0)))
        # Telescope tab's Slew & Center per-run fields start out matching
        # the persisted defaults too.
        self.sc_exposure_var.set(str(c.get("centering_exposure_seconds", 2.0)))
        self.sc_tolerance_var.set(str(c.get("centering_tolerance_arcmin", 5.0)))
        self.sc_iterations_var.set(str(c.get("centering_max_iterations", 3)))
        self._update_camera_options()
        if not self.astap_path_var.get():
            self.astap_status_var.set("(not configured -- Browse or Detect Automatically)")

    def on_settings_save(self):
        raw = {
            "client_id": self.set_client_id_var.get().strip(),
            "discovery_timeout": self.set_discovery_timeout_var.get().strip(),
            "preferred_server_ip": self.set_pref_ip_var.get().strip(),
            "preferred_server_port": self.set_pref_port_var.get().strip(),
            "output_directory": self.output_dir_var.get().strip(),
            "astap_path": self.astap_path_var.get().strip(),
            "plate_solve_timeout": self.plate_solve_timeout_var.get().strip(),
            "centering_tolerance_arcmin": self.centering_tolerance_var.get().strip(),
            "centering_max_iterations": self.centering_iterations_var.get().strip(),
            "centering_exposure_seconds": self.centering_exposure_var.get().strip(),
            "centering_camera": str(self._parse_camera_number(self.centering_camera_var.get())),
            "minimum_target_altitude_deg": self.min_altitude_var.get().strip(),
            "sun_exclusion_deg": self.sun_exclusion_var.get().strip(),
        }
        ok, errors, parsed = validate_settings(raw)
        if not ok:
            message = "Settings NOT saved -- fix these first:\n\n" + "\n".join(errors)
            self.settings_status_var.set("Save FAILED -- see error dialog")
            messagebox.showerror("Invalid settings", message)
            return

        # self.cfg IS self.state.cfg (same dict object, set up in __init__),
        # so this single update() already takes effect for both -- and for
        # anything that reads state.cfg fresh at point of use (output
        # folder, ASTAP path, Slew & Center defaults) with no restart
        # needed. client_id/discovery_timeout only affect the NEXT
        # Discover, since already-connected device objects keep the
        # client_id they were constructed with.
        self.cfg.update(parsed)
        try:
            save_config(self.cfg)
        except OSError as e:
            self.settings_status_var.set(f"Save FAILED: {e}")
            return

        self.settings_status_var.set("Settings saved.")
        self.submit(lambda: print("Settings saved to config.json."))

    def on_settings_reload(self):
        self.cfg.clear()
        self.cfg.update(load_config())
        self._populate_settings_widgets()
        self.settings_status_var.set("Reloaded from config.json.")

    def on_astap_browse(self):
        current = self.astap_path_var.get().strip()
        chosen = filedialog.askopenfilename(
            title="Choose the ASTAP executable",
            initialdir=os.path.dirname(current) if current else "C:\\",
            filetypes=[("Executable", "*.exe"), ("All files", "*.*")])
        if chosen:
            self.astap_path_var.set(chosen)
            self.astap_status_var.set("(not tested yet -- click Test ASTAP)")

    def on_astap_detect(self):
        def task():
            found = platesolver.find_astap()
            print(f"ASTAP detected: {found}" if found else
                  "No ASTAP installation found on PATH or in common install locations.")
            self.ui(lambda: self._apply_astap_detected(found))
        self.submit(task)

    def _apply_astap_detected(self, found):
        if not found:
            self.astap_status_var.set("Not found automatically -- Browse for it manually.")
            return
        current = self.astap_path_var.get().strip()
        if current and os.path.normcase(current) == os.path.normcase(found):
            self.astap_status_var.set(f"Detected (already set): {found}")
            return
        # Never overwrite a saved path silently -- ask first.
        if messagebox.askyesno("ASTAP detected", f"Found ASTAP at:\n{found}\n\nUse this path?"):
            self.astap_path_var.set(found)
            self.astap_status_var.set(f"Using detected path: {found}")
        else:
            self.astap_status_var.set(f"Detected but not applied: {found}")

    def on_astap_test(self):
        path = self.astap_path_var.get().strip()

        def task():
            result = platesolver.test_astap(path)
            if result["ok"]:
                print(f"ASTAP executable: OK\nPath: {path}\n"
                      f"Version: {result.get('version') or '(unknown)'}")
            else:
                print(f"ASTAP test FAILED:\n{result['message']}")
            self.ui(lambda: self._apply_astap_test_result(result, path))
        self.submit(task)

    def _apply_astap_test_result(self, result, path):
        if result["ok"]:
            ver = result.get("version") or "unknown version"
            self.astap_status_var.set(f"\u2713 OK -- {ver}  ({path})")
        else:
            self.astap_status_var.set(f"\u2717 FAILED: {result['message']}")

    # ------------------------------------------------------------------
    # Slew & Center
    # ------------------------------------------------------------------

    def on_slew_and_center(self):
        ra_raw, dec_raw = self.ra_var.get().strip(), self.dec_var.get().strip()
        target_name = self.target_var.get()

        try:
            camera_number = self._parse_camera_number(self.sc_camera_var.get())
            exposure = float(self.sc_exposure_var.get())
            tolerance = float(self.sc_tolerance_var.get())
            iterations = int(self.sc_iterations_var.get())
        except ValueError:
            self.submit(lambda: print("Invalid Slew & Center parameter "
                                       "(exposure/tolerance/iterations)."))
            return

        if target_name in console.CATALOG:
            ra, dec = console.CATALOG[target_name]
        elif ra_raw and dec_raw:
            try:
                ra, dec = float(ra_raw), float(dec_raw)
            except ValueError:
                self.submit(lambda: print("Invalid RA/Dec for Slew & Center."))
                return
        else:
            self.submit(lambda: print(
                "Pick a catalog target or enter RA/Dec before running Slew & Center."))
            return

        astap_path = self.cfg.get("astap_path", "")
        plate_solve_timeout = self.cfg.get("plate_solve_timeout", 60)
        min_alt = self.cfg.get("minimum_target_altitude_deg", 20.0)
        sun_excl = self.cfg.get("sun_exclusion_deg", 30.0)
        out_dir = self.output_dir_var.get().strip() or None

        self._sc_abort_event = threading.Event()
        abort_event = self._sc_abort_event

        def progress(payload):
            self.ui(lambda: self._apply_centering_progress(payload))

        def task():
            result = centering.slew_and_center(
                self.state, ra, dec, camera_number, exposure, tolerance, iterations,
                astap_path, plate_solve_timeout=plate_solve_timeout,
                min_altitude_deg=min_alt, sun_exclusion_deg=sun_excl,
                output_dir=out_dir, progress_cb=progress, abort_event=abort_event,
            )
            print(f"\nSlew & Center finished: {'SUCCESS' if result['success'] else 'DID NOT COMPLETE'}")
            print(result["message"])
        self.submit(task)

    def on_slew_and_center_abort(self):
        if self._sc_abort_event is not None and not self._sc_abort_event.is_set():
            self._sc_abort_event.set()
            self.log_queue.put("Abort requested -- will stop at the next safe checkpoint.")
        else:
            self.log_queue.put("No Slew & Center run in progress.")

    def _apply_centering_progress(self, payload):
        status = payload.get("status", "")
        if "iteration" in payload:
            self.sc_progress_vars["Iteration"].set(
                f"{payload['iteration']}/{payload.get('max_iterations', self.sc_iterations_var.get())}")
        if "target_ra" in payload and "target_dec" in payload:
            self.sc_progress_vars["Target"].set(
                f"{payload['target_ra']:.4f}h  {payload['target_dec']:.3f}deg")
        if "solved_ra" in payload and "solved_dec" in payload:
            self.sc_progress_vars["Solved"].set(
                f"{payload['solved_ra']:.4f}h  {payload['solved_dec']:.3f}deg")
        if "error_arcmin" in payload:
            self.sc_progress_vars["Pointing error"].set(f"{payload['error_arcmin']:.2f} arcmin")
        status_labels = {
            "slewing": "SLEWING", "iteration_start": "SOLVING", "solved": "SOLVED",
            "centered": "CENTERED", "max_iterations": "NOT CENTERED (max iterations)",
            "error": "ERROR", "rejected": "REJECTED (safety)", "solve_failed": "SOLVE FAILED",
            "sync": "SYNCING", "aborted": "ABORTED",
        }
        self.sc_progress_vars["Status"].set(status_labels.get(status, status.upper()))

    # ------------------------------------------------------------------
    # Diagnostics handlers
    # ------------------------------------------------------------------

    def on_show_capabilities(self):
        def task():
            if not self.state.devices:
                print("No devices known -- run Discover first.")
                return
            diagnostics.print_capability_report(list(self.state.devices.values()))
        self.submit(task)

    def on_show_supported_actions(self):
        self.submit(lambda: console.show_supported_actions(self.state))

    def on_dump_device_info(self):
        self.submit(lambda: console.dump_device_info(self.state))

    def on_show_summary(self):
        def task():
            diagnostics.print_diagnostic_summary(
                self.state.software_info, self.state.slew_test_results,
                self.state.moveaxis_test_results)
        self.submit(task)

    def on_raw_get(self):
        dtype, dnum, member, params_raw = (self.raw_type_var.get(), self.raw_num_var.get(),
                                            self.raw_member_var.get(), self.raw_params_var.get())

        def task():
            try:
                dev = self.state.devices.get((dtype, int(dnum)))
            except ValueError:
                print("Invalid device number.")
                return
            if dev is None:
                print(f"No such device known ({dtype} {dnum}).")
                return
            params = console._parse_kv(params_raw)
            status, body, elapsed = dev.raw_get(member, **params)
            print(f"GET {dev._url(member)}")
            print(f"HTTP {status}  ({elapsed:.1f}ms)")
            print(body)
        self.submit(task)

    def on_raw_put(self):
        dtype, dnum, member, params_raw = (self.raw_type_var.get(), self.raw_num_var.get(),
                                            self.raw_member_var.get(), self.raw_params_var.get())

        def task():
            try:
                dev = self.state.devices.get((dtype, int(dnum)))
            except ValueError:
                print("Invalid device number.")
                return
            if dev is None:
                print(f"No such device known ({dtype} {dnum}).")
                return
            data = console._parse_kv(params_raw)
            status, body, elapsed = dev.raw_put(member, **data)
            print(f"PUT {dev._url(member)}  data={data}")
            print(f"HTTP {status}  ({elapsed:.1f}ms)")
            print(body)
        self.submit(task)


def main():
    console.setup_logging()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
