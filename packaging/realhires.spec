# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: one folder containing the GUI (UniversalMusicPlayer) and the CLI
# (realhires-cli), sharing one runtime, plus a bundled FFmpeg in _internal/ffmpeg.
# On macOS the folder is wrapped into UniversalMusicPlayer.app.
#
# Build:  python packaging/fetch_ffmpeg.py && pyinstaller --noconfirm packaging/realhires.spec
import glob
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))
FFMPEG_DIR = os.environ.get("REALHIRES_FFMPEG_BUNDLE", os.path.join(ROOT, "build", "ffmpeg"))
EXT = ".exe" if sys.platform == "win32" else ""
with open(os.path.join(ROOT, "realhires", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__ = "([^"]+)"', fh.read()).group(1)

ffmpeg_bins = []
for name in ("ffmpeg", "ffprobe"):
    path = os.path.join(FFMPEG_DIR, name + EXT)
    if os.path.isfile(path):
        ffmpeg_bins.append((path, "ffmpeg"))
    else:
        print(f"WARNING: {path} not found - the package will need FFmpeg installed on the system")
ffmpeg_docs = [(p, "ffmpeg") for p in glob.glob(os.path.join(FFMPEG_DIR, "*.txt"))]
ICON_PNG = os.path.join(ROOT, "realhires", "assets", "icon.png")
assets = [(ICON_PNG, os.path.join("realhires", "assets"))]
# 2.0: the Inter typeface (SIL Open Font License, its licence travels with it), loaded
# privately by the player on Windows and Linux (realhires/player_theme.py)
assets += [(p, os.path.join("realhires", "assets", "fonts"))
           for p in glob.glob(os.path.join(ROOT, "realhires", "assets", "fonts", "*"))]
# 2.1: the translations (realhires/i18n.py reads them from realhires/assets/lang)
assets += [(p, os.path.join("realhires", "assets", "lang"))
           for p in glob.glob(os.path.join(ROOT, "realhires", "assets", "lang", "*.json"))]
IS_WIN = sys.platform == "win32"
# PyInstaller converts the PNG to .ico/.icns itself when Pillow is installed.
try:
    from PIL import Image
    exe_icon = ICON_PNG
except ImportError:
    Image = None
    exe_icon = None
    print("WARNING: Pillow not installed - building without an application icon")
if IS_WIN and Image is not None:
    # Uncompressed (BMP) icon images: PNG-compressed icons are high-entropy resources,
    # one of the traits antivirus heuristics count against an executable.
    exe_icon = os.path.join(ROOT, "build", "realhires.ico")
    os.makedirs(os.path.dirname(exe_icon), exist_ok=True)
    Image.open(ICON_PNG).convert("RGBA").save(
        exe_icon, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
        bitmap_format="bmp")

# Windows: keep the compressed program archive out of the .exe (it is written next to
# it as <name>.pkg), so the .exe is only the launcher, and give both executables
# version information.  A bare launcher with an appended high-entropy archive and no
# version resource is what antivirus heuristics most often mistake for a packed
# trojan.  REALHIRES_APPEND_PKG=0/1 overrides the default (for testing).
APPEND_PKG = os.environ.get("REALHIRES_APPEND_PKG", "0" if IS_WIN else "1") == "1"


def version_info(name, description):
    if not IS_WIN:
        return None
    from PyInstaller.utils.win32.versioninfo import (FixedFileInfo, StringFileInfo, StringStruct,
                                                    StringTable, VarFileInfo, VarStruct, VSVersionInfo)
    nums = tuple(int(x) for x in (VERSION.split(".") + ["0", "0", "0"])[:3]) + (0,)
    strings = [("CompanyName", "SU2H1"), ("FileDescription", description), ("FileVersion", VERSION),
               ("InternalName", name), ("LegalCopyright", "Copyright (c) SU2H1"),
               ("OriginalFilename", name + ".exe"), ("ProductName", "Universal Music Player"),
               ("ProductVersion", VERSION), ("Comments", "https://github.com/SU2H1/Universal-Music-Player")]
    return VSVersionInfo(
        ffi=FixedFileInfo(filevers=nums, prodvers=nums, mask=0x3F, flags=0x0, OS=0x40004, fileType=0x1,
                          subtype=0x0, date=(0, 0)),
        kids=[StringFileInfo([StringTable("040904B0", [StringStruct(k, v) for k, v in strings])]),
              VarFileInfo([VarStruct("Translation", [0x0409, 1200])])])
# Oldest macOS the app declares support for; CI verifies every bundled binary
# against it with packaging/check_compat.py.
MACOS_MIN = os.environ.get("REALHIRES_MACOS_MIN", "12.0")

EXCLUDES = ["matplotlib", "scipy", "pandas", "PIL", "IPython", "pytest", "setuptools", "pip"]

# 2.0: the player's sound output is miniaudio, a cffi extension (_miniaudio) whose C code
# imports _cffi_backend, which PyInstaller cannot see; realhires.player imports miniaudio
# inside a function.
PLAYER_IMPORTS = ["miniaudio", "_miniaudio", "_cffi_backend"]
# miniaudio is MIT-licensed: its licence notice (in its package metadata) travels with it
try:
    from PyInstaller.utils.hooks import copy_metadata
    assets += copy_metadata("miniaudio")
except Exception:  # noqa: BLE001 - building without the player's sound output
    pass
gui_a = Analysis([os.path.join(SPECPATH, "entry_gui.py")], pathex=[ROOT],
                 binaries=ffmpeg_bins, datas=ffmpeg_docs + assets, excludes=EXCLUDES,
                 hiddenimports=PLAYER_IMPORTS)
cli_a = Analysis([os.path.join(SPECPATH, "entry_cli.py")], pathex=[ROOT],
                 excludes=EXCLUDES + ["tkinter", "_tkinter"] + PLAYER_IMPORTS + ["cffi"])

if sys.platform.startswith("linux"):
    # Use the desktop's own fontconfig/freetype: they are on every Linux desktop,
    # and an old bundled fontconfig cannot read newer distributions' config files.
    SYSTEM_LIBS = ("libfontconfig.so", "libfreetype.so")
    gui_a.binaries = [b for b in gui_a.binaries if not os.path.basename(b[0]).startswith(SYSTEM_LIBS)]

gui_exe = EXE(PYZ(gui_a.pure), gui_a.scripts, [], exclude_binaries=True, name="UniversalMusicPlayer",
              console=False, upx=False, icon=exe_icon, append_pkg=APPEND_PKG,
              version=version_info("UniversalMusicPlayer", "Universal Music Player - a music player that checks whether hi-res audio is genuine"))
# Different name from the GUI on purpose: Windows and macOS file systems are
# case-insensitive, so "realhires" would clash with "RealHiRes" (1.x's GUI name).
cli_exe = EXE(PYZ(cli_a.pure), cli_a.scripts, [], exclude_binaries=True, name="realhires-cli",
              console=True, upx=False, icon=exe_icon, append_pkg=APPEND_PKG,
              version=version_info("realhires-cli", "Universal Music Player command-line tool (Real-Hi-Res quality checker)"))

# The GUI must come first: on macOS the first executable becomes the app's main program.
coll = COLLECT(gui_exe, gui_a.binaries, gui_a.datas, cli_exe, cli_a.binaries, cli_a.datas,
               upx=False, name="UniversalMusicPlayer")

if sys.platform == "darwin":
    app = BUNDLE(coll, name="UniversalMusicPlayer.app", bundle_identifier="io.github.su2h1.universalmusicplayer",
                 version=VERSION, icon=exe_icon,
                 info_plist={"NSHighResolutionCapable": True, "CFBundleShortVersionString": VERSION,
                             "CFBundleVersion": VERSION, "LSMinimumSystemVersion": MACOS_MIN,
                             "CFBundleDisplayName": "Universal Music Player"})
