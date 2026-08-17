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
from tkinter import filedialog, ttk

import numpy as np
from PIL import Image, ImageTk

import diagnostics
import imaging
import main as console
from alpaca import AlpacaError
from config import load_config, save_config

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
        self.tab_diagnostics = ttk.Frame(notebook, padding=8)

        notebook.add(self.tab_telescope, text="Telescope")
        notebook.add(self.tab_camera, text="Camera")
        notebook.add(self.tab_focuser, text="Focuser")
        notebook.add(self.tab_filterwheel, text="Filter Wheel")
        notebook.add(self.tab_switch, text="Switch")
        notebook.add(self.tab_diagnostics, text="Diagnostics / Raw")

        self._build_telescope_tab()
        self._build_camera_tab()
        self._build_focuser_tab()
        self._build_filterwheel_tab()
        self._build_switch_tab()
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
