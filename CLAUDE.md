# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

SD Kaart Manager is a Dutch-language Windows GUI desktop application for automatically writing firmware versions to SD cards. Built with Python + CustomTkinter.

## Commands

**Install dependencies:**
```bash
pip install customtkinter psutil Pillow pyinstaller
```

**Run the application:**
```bash
python sd_manager.py
```

**Build Windows executable:**
```bash
build.bat
```
Output: `dist/SD_Kaart_Manager.exe`

There are no automated tests configured.

## Architecture

The entire application lives in a single file: `sd_manager.py` (~723 lines). It is structured into clear functional sections:

1. **Configuration** (top of file) — `DEFAULT_CONFIG`, `load_config()`, `save_config()` read/write `sd_manager_config.json` next to the executable.

2. **Drive Detection** — `get_removable_drives()` uses `psutil` with a Windows WMIC fallback to find removable drives.

3. **Validation** — `validate_drive()` checks file extensions (whitelist), file count, drive size (default max 5 GB), subdirectories, and system folders before any write operation. This is the core safety layer.

4. **Formatting & Copying** — `format_drive()` calls Windows `format.com` with FAT32 (≤32 GB) or exFAT (>32 GB). `copy_version_to_drive()` does a recursive copy from the selected source version directory.

5. **GUI (`App` class)** — Single CTkinter window (780×620). Long-running operations (format, copy) run in background threads via `threading.Thread`. Drive detection polls every 2 seconds.

## Version Metadata

Firmware versions are subdirectories inside the configured source folder. An optional `versions.json` in that folder provides Dutch-language metadata:

```json
{
  "version_name": {
    "omschrijving": "Description",
    "functie": "Function/purpose"
  }
}
```

## Key Configuration Defaults

| Setting | Default | Purpose |
|---|---|---|
| `max_drive_gb` | 5.0 | Prevents accidentally targeting large drives |
| `allowed_extensions` | `.bin .hex .dat` | Firmware file whitelist |
| `auto_start` | false | Auto-write on drive insert |
| `auto_format_corrupt` | false | Auto-format unreadable drives |

## Historical Versions

`versies/` contains numbered previous versions (`sd_manager (1-8).py`) and `sd_manager_ok (5).py` as a stable reference. These are backup copies only.
