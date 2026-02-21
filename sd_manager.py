import customtkinter as ctk
import tkinter as tk
from tkinter import filedialog, messagebox
import json
import os
import shutil
import threading
import psutil
import subprocess
import time
from pathlib import Path
from datetime import datetime

# ── Thema ──────────────────────────────────────────────────────────────────────
ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")

CONFIG_FILE = "sd_manager_config.json"

DEFAULT_CONFIG = {
    "source_dir": "",
    "allowed_extensions": [".bin", ".hex", ".dat"],
    "max_files": 100,
    "auto_start": False,
    "allow_subdirs": False,
    "last_version": "",
    "max_drive_gb": 5.0,
    "auto_format_corrupt": False,
}


# ── Hulpfuncties ───────────────────────────────────────────────────────────────

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                # merge met defaults voor ontbrekende sleutels
                for k, v in DEFAULT_CONFIG.items():
                    cfg.setdefault(k, v)
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()


def save_config(cfg):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)


def get_removable_drives():
    """Geeft lijst van verwisselbare schijven terug als (letter, label, size_gb).
    Lege slots (geen media) worden gefilterd."""
    drives = []
    for part in psutil.disk_partitions(all=False):
        if "removable" in part.opts or part.fstype in ("FAT32", "FAT", "exFAT"):
            try:
                usage = psutil.disk_usage(part.mountpoint)
                size_gb = usage.total / (1024 ** 3)
                if size_gb < 0.01:  # lege slot in multicard lezer
                    continue
                label = part.mountpoint.rstrip("\\")
                drives.append((label, f"{label}  [{size_gb:.1f} GB]", size_gb))
            except Exception:
                pass  # niet mountbaar = lege slot, overslaan
    # Windows-specifieke fallback via wmic
    if not drives:
        try:
            result = subprocess.run(
                ["wmic", "logicaldisk", "where", "drivetype=2", "get",
                 "deviceid,volumename,size", "/format:csv"],
                capture_output=True, text=True, timeout=5
            )
            for line in result.stdout.splitlines():
                parts = line.strip().split(",")
                if len(parts) >= 4 and parts[1]:
                    letter = parts[1].strip()
                    name = parts[3].strip() or "Geen label"
                    try:
                        size_gb = int(parts[2].strip()) / (1024 ** 3)
                        if size_gb < 0.01:
                            continue
                        drives.append((letter, f"{letter}  {name}  [{size_gb:.1f} GB]", size_gb))
                    except Exception:
                        pass  # geen grootte = lege slot
        except Exception:
            pass
    return drives


def load_versions_json(source_dir):
    """Laad of maak het versions.json bestand in de hoofdmap."""
    json_path = Path(source_dir) / "versions.json"
    data = {}

    if json_path.exists():
        try:
            with open(json_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}

    # Submappen scannen en ontbrekende toevoegen
    changed = False
    for item in Path(source_dir).iterdir():
        if item.is_dir() and not item.name.startswith("."):
            if item.name not in data:
                data[item.name] = {"omschrijving": "", "functie": ""}
                changed = True

    if changed or not json_path.exists():
        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)

    return data, json_path


def validate_drive(drive_letter, allowed_exts, max_files, allow_subdirs=False, max_drive_gb=None, drive_size_gb=None):
    """
    Valideert of de drive veilig leeggemaakt mag worden.
    Geeft (ok: bool, reden: str) terug.
    Windows systeemmappen en verborgen items worden genegeerd.
    """
    # Windows systeemmappen die altijd aanwezig kunnen zijn op een SD-kaart
    SYSTEM_DIRS = {
        "system volume information",
        "$recycle.bin",
        "recycler",
        "$recyclebin",
        "found.000",
    }

    root = Path(drive_letter)
    try:
        all_items = list(root.iterdir())
    except PermissionError:
        return False, "Geen leesrechten op deze drive.", False
    except Exception:
        # Niet leesbaar = mogelijk corrupt
        return False, f"Drive {drive_letter} is niet leesbaar — mogelijk corrupt bestandssysteem.", True

    # Filter verborgen items en systeemmappen eruit
    def is_system(item):
        name_lower = item.name.lower()
        if name_lower in SYSTEM_DIRS:
            return True
        # verborgen bestanden/mappen (beginnen met $ of zijn hidden via attrib)
        if item.name.startswith("$") or item.name.startswith("."):
            return True
        return False

    real_items = [i for i in all_items if not is_system(i)]

    # Drive grootte check
    if max_drive_gb and drive_size_gb is not None:
        if drive_size_gb > max_drive_gb:
            return False, f"SD-kaart is {drive_size_gb:.1f} GB, maximaal toegestaan is {max_drive_gb:.1f} GB — mogelijk verkeerde drive.", False

    # Geen gebruikersmappen toegestaan (tenzij instelling aan staat)
    subdirs = [i for i in real_items if i.is_dir()]
    if subdirs and not allow_subdirs:
        return False, f"SD-kaart bevat submappen ({len(subdirs)}x) — mogelijk verkeerde drive. (Of schakel 'mappen toestaan' in bij Instellingen)", False

    files = [i for i in real_items if i.is_file()]

    # Max aantal bestanden
    if len(files) > max_files:
        return False, f"Drive bevat {len(files)} bestanden (max {max_files}) — mogelijk verkeerde drive.", False

    # Extensie check
    if allowed_exts:
        bad = [f for f in files if f.suffix.lower() not in allowed_exts]
        if bad:
            return False, f"Drive bevat niet-toegestane bestanden (bijv. {bad[0].name}) — mogelijk verkeerde drive.", False

    return True, "OK", False


def format_drive(drive_letter, log_cb, drive_size_gb=None):
    """Probeert drive te formatteren via Windows format commando."""
    # Kies bestandssysteem op basis van grootte: FAT32 voor ≤32 GB, exFAT voor groter
    fs = "FAT32" if (drive_size_gb is None or drive_size_gb <= 32) else "exFAT"
    log_cb(f"⚠️  Formatteren van {drive_letter} als {fs} wordt gestart...", "warning")
    try:
        format_exe = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "format.com")
        result = subprocess.run(
            [format_exe, drive_letter, f"/FS:{fs}", "/Q", "/Y"],
            capture_output=True, text=True, timeout=120
        )
        if result.returncode == 0:
            log_cb(f"✅  {drive_letter} succesvol geformatteerd als {fs}.", "success")
            return True
        else:
            log_cb(f"❌  Formatteren mislukt: {(result.stderr or result.stdout).strip()}", "error")
            return False
    except subprocess.TimeoutExpired:
        log_cb("❌  Formatteren duurde te lang.", "error")
        return False
    except Exception as e:
        log_cb(f"❌  Fout bij formatteren: {e}", "error")
        return False


def clear_drive(drive_letter, log_cb, drive_size_gb=None):
    """Verwijdert alle bestanden van de drive, probeert formatteren bij fouten."""
    root = Path(drive_letter)
    errors = []
    for item in root.iterdir():
        try:
            if item.is_file():
                item.unlink()
            elif item.is_dir():
                shutil.rmtree(item)
        except Exception as e:
            errors.append(str(e))

    if errors:
        log_cb(f"⚠️  Kon {len(errors)} item(s) niet verwijderen. Formatteren proberen...", "warning")
        return format_drive(drive_letter, log_cb, drive_size_gb=drive_size_gb)
    return True


def copy_version_to_drive(source_dir, version_name, drive_letter, log_cb):
    """Kopieert bestanden én mappen van de gekozen versie naar de drive."""
    src = Path(source_dir) / version_name
    dst = Path(drive_letter)
    copied_files = 0
    copied_dirs = 0

    for item in src.rglob("*"):
        relative = item.relative_to(src)
        target = dst / relative
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            copied_dirs += 1
        elif item.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied_files += 1

    now = datetime.now().strftime("%H:%M")
    dir_info = f", {copied_dirs} map(pen)" if copied_dirs else ""
    log_cb(f"✅  {copied_files} bestand(en){dir_info} vanuit [{version_name}] naar [{drive_letter}] geschreven om {now}.", "success")


# ── Hoofd applicatie ───────────────────────────────────────────────────────────

class App(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("SD Kaart Overschrijven")
        self.geometry("780x620")
        self.resizable(False, False)

        self.config = load_config()
        self.config["auto_start"] = False  # altijd uit bij opstarten
        self.versions_data = {}
        self.versions_json_path = None
        self.drive_poll_job = None
        self.known_drives = set()
        self._busy = False

        self._build_ui()
        self._load_source_if_set()
        self._poll_drives()
        self._center_window(self, 780, 620)

    def _center_window(self, win, width, height):
        """Centreert een venster op het scherm."""
        win.update_idletasks()
        sw = win.winfo_screenwidth()
        sh = win.winfo_screenheight()
        x = (sw - width) // 2
        y = (sh - height) // 2
        win.geometry(f"{width}x{height}+{x}+{y}")

    # ── UI opbouw ──────────────────────────────────────────────────────────────

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=1)

        # ── Titel ──
        ctk.CTkLabel(self, text="SD Kaart Overschrijven", font=ctk.CTkFont(size=22, weight="bold")).grid(
            row=0, column=0, pady=(12, 2), sticky="ew")

        # ── Bronmap sectie ──
        src_frame = ctk.CTkFrame(self)
        src_frame.grid(row=1, column=0, padx=20, pady=3, sticky="ew")
        src_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(src_frame, text="Bronmap:", width=100, anchor="w").grid(row=0, column=0, padx=12, pady=10)
        self.src_label = ctk.CTkLabel(src_frame, text=self.config["source_dir"] or "— nog niet gekozen —",
                                      anchor="w", wraplength=480)
        self.src_label.grid(row=0, column=1, padx=8, pady=10, sticky="ew")
        ctk.CTkButton(src_frame, text="Kiezen", width=90, command=self._choose_source).grid(
            row=0, column=2, padx=12, pady=10)

        # ── Versie dropdown ──
        ver_frame = ctk.CTkFrame(self)
        ver_frame.grid(row=2, column=0, padx=20, pady=3, sticky="ew")
        ver_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(ver_frame, text="Schrijf versie:", width=100, anchor="w").grid(row=0, column=0, padx=12, pady=10)
        self.version_var = ctk.StringVar(value="— kies versie om naar SD-kaart te schrijven —")
        self.version_menu = ctk.CTkOptionMenu(ver_frame, variable=self.version_var,
                                               values=["— kies versie om naar SD-kaart te schrijven —"],
                                               command=self._on_version_change)
        self.version_menu.grid(row=0, column=1, padx=8, pady=10, sticky="ew")
        ctk.CTkButton(ver_frame, text="Info bewerken", width=110, command=self._edit_version_info).grid(
            row=0, column=2, padx=12, pady=10)

        # ── Versie info ──
        self.ver_info_label = ctk.CTkLabel(self, text="", anchor="w", wraplength=720,
                                            text_color="gray70", font=ctk.CTkFont(size=12))
        self.ver_info_label.grid(row=3, column=0, padx=24, pady=(0, 4), sticky="ew")

        # ── Drive sectie ──
        drv_frame = ctk.CTkFrame(self)
        drv_frame.grid(row=4, column=0, padx=20, pady=3, sticky="ew")
        drv_frame.grid_columnconfigure(1, weight=1)

        ctk.CTkLabel(drv_frame, text="Doeldrive\n(SD-kaart):", width=100, anchor="w").grid(row=0, column=0, padx=12, pady=10)
        self.drive_var = ctk.StringVar(value="— geen verwisselbare SD-kaart gevonden —")
        self.drive_menu = ctk.CTkOptionMenu(drv_frame, variable=self.drive_var, values=["— geen verwisselbare SD-kaart gevonden —"])
        self.drive_menu.grid(row=0, column=1, padx=8, pady=10, sticky="ew")
        ctk.CTkButton(drv_frame, text="↻", width=40, command=self._refresh_drives).grid(
            row=0, column=2, padx=4, pady=10)

        # ── Rij 1: Start overschrijven + auto-start switch ──
        row1_frame = ctk.CTkFrame(self)
        row1_frame.grid(row=5, column=0, padx=20, pady=(6, 3), sticky="ew")
        row1_frame.grid_columnconfigure(0, weight=1)

        self.start_btn = ctk.CTkButton(row1_frame, text="▶  Start overschrijven", height=42,
                                        font=ctk.CTkFont(size=15, weight="bold"),
                                        command=self._start_process)
        self.start_btn.grid(row=0, column=0, padx=(12, 8), pady=10, sticky="ew")

        self.auto_var = ctk.BooleanVar(value=self.config["auto_start"])
        ctk.CTkSwitch(row1_frame, text="Automatisch starten\nbij drive detectie",
                      variable=self.auto_var, command=self._save_config).grid(
            row=0, column=1, padx=(4, 16), pady=10, sticky="e")

        # ── Rij 2: Formateer knop + corrupt-switch ──
        row2_frame = ctk.CTkFrame(self)
        row2_frame.grid(row=6, column=0, padx=20, pady=(3, 6), sticky="ew")
        row2_frame.grid_columnconfigure(0, weight=1)

        self.format_btn = ctk.CTkButton(row2_frame, text="🗂  Formatteer geselecteerde drive", height=38,
                                         fg_color="gray30", hover_color="#b45309",
                                         font=ctk.CTkFont(size=13),
                                         command=self._format_selected_drive)
        self.format_btn.grid(row=0, column=0, padx=(12, 8), pady=10, sticky="ew")

        self.auto_format_var = ctk.BooleanVar(value=self.config.get("auto_format_corrupt", False))
        self.auto_format_switch = ctk.CTkSwitch(row2_frame, text="Automatisch formatteren\nbij corrupte SD-kaart",
                                                  variable=self.auto_format_var,
                                                  command=self._on_auto_format_toggle)
        self.auto_format_switch.grid(row=0, column=1, padx=(4, 16), pady=10, sticky="e")
        self._update_auto_format_switch_state()

        # ── Log venster ──
        log_frame = ctk.CTkFrame(self)
        log_frame.grid(row=7, column=0, padx=20, pady=(4, 6), sticky="ew")
        log_frame.grid_columnconfigure(0, weight=1)

        ctk.CTkLabel(log_frame, text="Meldingen", anchor="w",
                     font=ctk.CTkFont(weight="bold")).grid(row=0, column=0, padx=12, pady=(8, 2), sticky="w")
        self.log_box = ctk.CTkTextbox(log_frame, height=130, state="disabled",
                                       font=ctk.CTkFont(family="Consolas", size=12))
        self.log_box.grid(row=1, column=0, padx=12, pady=(0, 12), sticky="ew")

        # kleurtags instellen via onderliggende tk widget
        self.log_box._textbox.tag_config("success", foreground="#4ade80")
        self.log_box._textbox.tag_config("warning", foreground="#facc15")
        self.log_box._textbox.tag_config("error", foreground="#f87171")
        self.log_box._textbox.tag_config("info", foreground="#93c5fd")

        # ── Instellingen knop ──
        ctk.CTkButton(self, text="⚙  Instellingen", fg_color="gray30", hover_color="gray40",
                      command=self._open_settings).grid(row=8, column=0, padx=20, pady=(0, 28), sticky="e")

    # ── Logging ───────────────────────────────────────────────────────────────

    def log(self, message, kind="info"):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}]  {message}\n"
        self.log_box.configure(state="normal")
        self.log_box._textbox.insert("end", line, kind)
        self.log_box._textbox.see("end")
        self.log_box.configure(state="disabled")

    # ── Drive polling ──────────────────────────────────────────────────────────

    def _poll_drives(self):
        current_drives = get_removable_drives()
        current = set(d[0] for d in current_drives)
        new_drives = current - self.known_drives
        removed_drives = self.known_drives - current

        if new_drives:
            for d in new_drives:
                self.log(f"🔌  Nieuwe drive gevonden: {d}", "info")
            self._refresh_drives(current_drives)
            if self.auto_var.get() and not self._busy:
                self._start_process()

        if removed_drives:
            for d in removed_drives:
                self.log(f"📤  Drive verwijderd: {d}", "warning")
            self._refresh_drives(current_drives)

        # Alleen updaten als er iets veranderd is
        if new_drives or removed_drives:
            self._update_auto_format_switch_state()

        self.known_drives = current
        self.drive_poll_job = self.after(2000, self._poll_drives)

    def _refresh_drives(self, drives=None):
        if drives is None:
            drives = get_removable_drives()
        current_selection = self.drive_var.get()
        if drives:
            labels = [d[1] for d in drives]
            letters = [d[0] for d in drives]
            sizes = [d[2] for d in drives]
            self.drive_menu.configure(values=labels)
            self._drive_map = dict(zip(labels, letters))
            self._drive_size_map = dict(zip(labels, sizes))
            # Bewaar huidige selectie als die nog bestaat, anders eerste
            if current_selection in labels:
                self.drive_var.set(current_selection)
            else:
                self.drive_var.set(labels[0])
        else:
            self.drive_menu.configure(values=["— geen verwisselbare SD-kaart gevonden —"])
            self.drive_var.set("— geen verwisselbare SD-kaart gevonden —")
            self._drive_map = {}
            self._drive_size_map = {}

    def _get_selected_drive(self):
        return getattr(self, "_drive_map", {}).get(self.drive_var.get(), None)

    def _get_selected_drive_size(self):
        return getattr(self, "_drive_size_map", {}).get(self.drive_var.get(), None)

    # ── Bronmap ───────────────────────────────────────────────────────────────

    def _choose_source(self):
        folder = filedialog.askdirectory(title="Kies de hoofdmap met versies")
        if folder:
            self.config["source_dir"] = folder
            save_config(self.config)
            self.src_label.configure(text=folder)
            self._load_source_if_set()

    def _load_source_if_set(self):
        src = self.config.get("source_dir", "")
        if src and os.path.isdir(src):
            self.versions_data, self.versions_json_path = load_versions_json(src)
            versions = [k for k in self.versions_data.keys()]
            if versions:
                self.version_menu.configure(values=versions)
                # herstel laatste versie, of val terug op eerste
                last = self.config.get("last_version", "")
                selected = last if last in versions else versions[0]
                self.version_var.set(selected)
                self._on_version_change(selected)
            else:
                self.version_menu.configure(values=["— geen submappen gevonden —"])
                self.version_var.set("— geen submappen gevonden —")

    def _on_version_change(self, choice):
        # Onthoud de gekozen versie
        if choice in self.versions_data:
            self.config["last_version"] = choice
            save_config(self.config)
        info = self.versions_data.get(choice, {})
        omschr = info.get("omschrijving", "")
        functie = info.get("functie", "")
        parts = []
        if omschr:
            parts.append(omschr)
        if functie:
            parts.append(f"Functie: {functie}")
        self.ver_info_label.configure(text="  ".join(parts) if parts else "Geen beschrijving.")

    # ── Versie info bewerken ───────────────────────────────────────────────────

    def _edit_version_info(self):
        version = self.version_var.get()
        if version not in self.versions_data:
            return

        win = ctk.CTkToplevel(self)
        win.title(f"Info bewerken — {version}")
        win.geometry("440x240")
        win.grab_set()
        self._center_window(win, 440, 240)

        ctk.CTkLabel(win, text="Omschrijving:").grid(row=0, column=0, padx=16, pady=(16, 4), sticky="w")
        omschr_entry = ctk.CTkEntry(win, width=350)
        omschr_entry.insert(0, self.versions_data[version].get("omschrijving", ""))
        omschr_entry.grid(row=1, column=0, padx=16, pady=4, sticky="ew")

        ctk.CTkLabel(win, text="Functie:").grid(row=2, column=0, padx=16, pady=(12, 4), sticky="w")
        functie_entry = ctk.CTkEntry(win, width=350)
        functie_entry.insert(0, self.versions_data[version].get("functie", ""))
        functie_entry.grid(row=3, column=0, padx=16, pady=4, sticky="ew")

        def save():
            self.versions_data[version]["omschrijving"] = omschr_entry.get()
            self.versions_data[version]["functie"] = functie_entry.get()
            with open(self.versions_json_path, "w", encoding="utf-8") as f:
                json.dump(self.versions_data, f, indent=2, ensure_ascii=False)
            self._on_version_change(version)
            win.destroy()

        ctk.CTkButton(win, text="Opslaan", command=save).grid(row=4, column=0, padx=16, pady=16, sticky="e")
        win.grid_columnconfigure(0, weight=1)

    # ── Hoofdproces ───────────────────────────────────────────────────────────

    def _start_process(self):
        if self._busy:
            return

        drive = self._get_selected_drive()
        version = self.version_var.get()
        src = self.config.get("source_dir", "")

        if not drive:
            self.log("❌  Geen geldige drive geselecteerd.", "error")
            return
        if not src or not os.path.isdir(src):
            self.log("❌  Hoofdmap niet ingesteld of niet gevonden.", "error")
            return
        if version not in self.versions_data:
            self.log("❌  Geen geldige versie gekozen.", "error")
            return

        self._busy = True
        self.start_btn.configure(state="disabled", text="Bezig...")
        threading.Thread(target=self._process_thread, args=(drive, version, src), daemon=True).start()

    def _process_thread(self, drive, version, src):
        allowed_exts = self.config.get("allowed_extensions", [])
        max_files = self.config.get("max_files", 100)
        max_drive_gb = self.config.get("max_drive_gb", 5.0)
        drive_size_gb = self._get_selected_drive_size()

        # 1. Valideer drive
        ok, reason, is_corrupt = validate_drive(drive, allowed_exts, max_files,
                                                 allow_subdirs=self.config.get("allow_subdirs", False),
                                                 max_drive_gb=max_drive_gb,
                                                 drive_size_gb=drive_size_gb)
        if not ok:
            self.after(0, self.log, f"🛑  Drive validatie mislukt: {reason}", "error")
            if is_corrupt and self.config.get("auto_format_corrupt", False):
                self.after(0, self.log, "🗂️   Corrupte SD-kaart gedetecteerd — automatisch formatteren...", "warning")
                format_ok = format_drive(drive, lambda m, k="warning": self.after(0, self.log, m, k),
                                          drive_size_gb=drive_size_gb)
                if not format_ok:
                    self.after(0, self.log, "❌  Automatisch formatteren mislukt. Probeer als administrator.", "error")
                    self.after(0, self._reset_busy)
                    return
                self.after(0, self.log, f"✅  {drive} geformatteerd, doorgaan met kopiëren...", "success")
            else:
                self.after(0, self.log, "Schrijven naar deze drive is niet mogelijk.", "error")
                self.after(0, self._reset_busy)
                return

        # 2. Leegmaken
        self.after(0, self.log, f"🗑️   Drive wordt leeg gemaakt: {drive}", "info")
        ok = clear_drive(drive, lambda m, k="warning": self.after(0, self.log, m, k), drive_size_gb=drive_size_gb)
        if not ok:
            self.after(0, self.log, f"❌  Kon {drive} niet leegmaken.", "error")
            self.after(0, self._reset_busy)
            return

        # 3. Kopiëren
        self.after(0, self.log, f"📋  Nieuwe bestanden worden gekopieerd vanuit [{version}]...", "info")
        try:
            copy_version_to_drive(src, version, drive,
                                   lambda m, k="success": self.after(0, self.log, m, k))
        except Exception as e:
            self.after(0, self.log, f"❌  Fout bij kopiëren: {e}", "error")

        self.after(0, self._reset_busy)

    def _reset_busy(self):
        self._busy = False
        self.start_btn.configure(state="normal", text="▶  Start overschrijven")
        self.format_btn.configure(state="normal", text="🗂  Formatteer geselecteerde drive")

    def _update_auto_format_switch_state(self):
        """Schakel de corrupt-switch alleen in als grootte-limiet ingesteld is én drive uitgelezen kan worden."""
        max_gb = self.config.get("max_drive_gb", None)
        drive_size = self._get_selected_drive_size()
        if max_gb and drive_size is not None:
            self.auto_format_switch.configure(state="normal")
        else:
            self.auto_format_var.set(False)
            self.auto_format_switch.configure(state="disabled")

    def _on_auto_format_toggle(self):
        self.config["auto_format_corrupt"] = self.auto_format_var.get()
        save_config(self.config)

    def _format_selected_drive(self):
        """Handmatig formatteren van de geselecteerde drive met bevestiging."""
        if self._busy:
            return
        drive = self._get_selected_drive()
        if not drive:
            self.log("❌  Geen geldige drive geselecteerd.", "error")
            return

        drive_size = self._get_selected_drive_size()
        max_gb = self.config.get("max_drive_gb", None)

        # Grootte veiligheidscheck
        if max_gb and drive_size is not None and drive_size > max_gb:
            self.log(f"🛑  Formatteren geblokkeerd: drive is {drive_size:.1f} GB (max {max_gb:.1f} GB).", "error")
            return

        # Bevestigingsvenster
        win = ctk.CTkToplevel(self)
        win.title("Bevestiging")
        win.geometry("420x180")
        win.grab_set()
        win.grid_columnconfigure(0, weight=1)
        self._center_window(win, 420, 180)

        msg = f"Weet je zeker dat je {drive} wil formatteren?\nAlle data op de drive wordt gewist."
        if drive_size:
            msg += f"\n\nDrive grootte: {drive_size:.1f} GB"
        ctk.CTkLabel(win, text=msg, wraplength=380, justify="center").grid(
            row=0, column=0, columnspan=2, padx=20, pady=(20, 16))

        def confirm():
            win.destroy()
            self._busy = True
            self.format_btn.configure(state="disabled", text="Bezig met formatteren...")
            self.start_btn.configure(state="disabled")
            self.log(f"🗂️   Formatteren gestart voor {drive}...", "warning")
            threading.Thread(target=self._format_thread, args=(drive,), daemon=True).start()

        ctk.CTkButton(win, text="Ja, formatteren", fg_color="#b45309", hover_color="#92400e",
                      command=confirm).grid(row=1, column=0, padx=(20, 8), pady=10, sticky="ew")
        ctk.CTkButton(win, text="Annuleren", fg_color="gray30", hover_color="gray40",
                      command=win.destroy).grid(row=1, column=1, padx=(8, 20), pady=10, sticky="ew")

    def _format_thread(self, drive):
        drive_size_gb = self._get_selected_drive_size()
        ok = format_drive(drive, lambda m, k="warning": self.after(0, self.log, m, k),
                          drive_size_gb=drive_size_gb)
        if ok:
            self.after(0, self.log, f"✅  {drive} is klaar voor gebruik.", "success")
        else:
            self.after(0, self.log, f"❌  Formatteren van {drive} mislukt. Probeer als administrator.", "error")
        self.after(0, self._reset_busy)

    # ── Instellingen venster ───────────────────────────────────────────────────

    def _open_settings(self):
        win = ctk.CTkToplevel(self)
        win.title("Instellingen — SD-kaart (doeldrive)")
        win.geometry("500x520")
        win.grab_set()
        win.grid_columnconfigure(0, weight=1)
        self._center_window(win, 500, 520)

        ctk.CTkLabel(win, text="Instellingen voor de SD-kaart (doeldrive)",
                     font=ctk.CTkFont(weight="bold"), anchor="w").grid(
            row=0, column=0, padx=16, pady=(16, 10), sticky="w")

        ctk.CTkLabel(win, text="Toegestane bestandsextensies op SD-kaart (komma-gescheiden):",
                     anchor="w").grid(row=1, column=0, padx=16, pady=(4, 2), sticky="w")
        ext_entry = ctk.CTkEntry(win, width=440)
        ext_entry.insert(0, ", ".join(self.config.get("allowed_extensions", [])))
        ext_entry.grid(row=2, column=0, padx=16, pady=2, sticky="ew")
        ctk.CTkLabel(win, text="Laat leeg om alle extensies toe te staan op de SD-kaart.",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=3, column=0, padx=16, pady=(0, 4), sticky="w")

        ctk.CTkLabel(win, text="Maximum aantal bestanden op SD-kaart (veiligheidscheck):",
                     anchor="w").grid(row=4, column=0, padx=16, pady=(12, 2), sticky="w")
        max_entry = ctk.CTkEntry(win, width=120)
        max_entry.insert(0, str(self.config.get("max_files", 100)))
        max_entry.grid(row=5, column=0, padx=16, pady=2, sticky="w")
        ctk.CTkLabel(win, text="Overschrijdt de SD-kaart dit aantal, wordt het proces geblokkeerd.",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=6, column=0, padx=16, pady=(0, 4), sticky="w")

        ctk.CTkLabel(win, text="Maximum grootte SD-kaart in GB (veiligheidscheck):",
                     anchor="w").grid(row=7, column=0, padx=16, pady=(12, 2), sticky="w")
        maxgb_entry = ctk.CTkEntry(win, width=120)
        maxgb_entry.insert(0, str(self.config.get("max_drive_gb", 5.0)))
        maxgb_entry.grid(row=8, column=0, padx=16, pady=2, sticky="w")
        ctk.CTkLabel(win, text="Drives groter dan dit worden geblokkeerd (bijv. 5 voor max 5 GB).",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=9, column=0, padx=16, pady=(0, 4), sticky="w")

        subdirs_var = ctk.BooleanVar(value=self.config.get("allow_subdirs", False))
        ctk.CTkSwitch(win, text="Submappen toestaan op SD-kaart (doeldrive)",
                      variable=subdirs_var).grid(row=10, column=0, padx=16, pady=(16, 2), sticky="w")
        ctk.CTkLabel(win, text="Uit = veiligheidscheck blokkeert als SD-kaart submappen bevat.",
                     text_color="gray60", font=ctk.CTkFont(size=11)).grid(
            row=11, column=0, padx=16, pady=(0, 4), sticky="w")

        def save():
            raw_exts = ext_entry.get().strip()
            exts = [e.strip() for e in raw_exts.split(",") if e.strip()] if raw_exts else []
            exts = [e if e.startswith(".") else f".{e}" for e in exts]
            try:
                max_f = int(max_entry.get())
            except ValueError:
                max_f = 100
            try:
                max_gb = float(maxgb_entry.get())
            except ValueError:
                max_gb = 5.0

            self.config["allowed_extensions"] = exts
            self.config["max_files"] = max_f
            self.config["max_drive_gb"] = max_gb
            self.config["allow_subdirs"] = subdirs_var.get()
            save_config(self.config)
            win.destroy()
            self.log("⚙️   Instellingen opgeslagen.", "info")

        ctk.CTkButton(win, text="Opslaan", command=save).grid(
            row=12, column=0, padx=16, pady=20, sticky="e")

    # ── Config opslaan ────────────────────────────────────────────────────────

    def _save_config(self):
        self.config["auto_start"] = self.auto_var.get()
        save_config(self.config)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app = App()
    app.mainloop()