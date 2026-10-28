"""
main.py - Clash of Clans auto-attack bot (Google Play Games on PC)   [v2 - new UI]

Run:
    python main.py

Flow (new UI):
    home "Battle" button -> Multiplayer menu -> "Find a Match" (left Battle card)
    -> army screen "Attack!" (if shown) -> base scouting -> deploy -> battle
    -> Return Home -> repeat

Menu:
    1) Full auto                    - attacks forever, auto-deploys
    2) Full auto + storage check    - stops when your storages are full
    3) Auto attack (NO deploy)      - menus only, YOU deploy manually
    4) Test / calibrate detection   - no clicking, prints match scores
    5) Calibrate game area          - REQUIRED once (and after resizing the window)
    6) Preview deploy points        - draws where taps will land + tests hotkeys
    0) Exit

Emergency stop: slam the mouse into any screen corner (pyautogui fail-safe)
or press Ctrl+C in the terminal.
"""

import os
import csv
import re
import sys
import time
import json
import copy
import random
import signal
import argparse
from datetime import datetime

# =========================================================================
# RUNTIME FLAGS (set from CLI args in main())
# =========================================================================
USE_GPU = False
GPU_READY = False

# =========================================================================
# LAZY MODULE LOADING
# =========================================================================
pyautogui = None
cv2 = None
np = None
gw = None
RapidOCR = None
mss_mod = None
_ocr_engine = None


def _ensure_pyautogui():
    global pyautogui
    if pyautogui is None:
        print("  [deps] importing pyautogui...")
        import pyautogui as _pa
        pyautogui = _pa
        pyautogui.PAUSE = 0.0
        pyautogui.FAILSAFE = True


def _ensure_full_deps(need_ocr=False):
    global cv2, np, gw, RapidOCR
    _ensure_pyautogui()

    if cv2 is None:
        print("  [deps] importing opencv-python...")
        import cv2 as _cv
        cv2 = _cv

    if np is None:
        print("  [deps] importing numpy...")
        import numpy as _np
        np = _np

    if gw is None:
        print("  [deps] importing pygetwindow...")
        import pygetwindow as _gw
        gw = _gw

    if need_ocr and RapidOCR is None:
        try:
            print("  [deps] importing rapidocr-onnxruntime (may take ~2s)...")
            from rapidocr_onnxruntime import RapidOCR as _r
            RapidOCR = _r
        except ImportError:
            print("\n  [!] rapidocr-onnxruntime not installed.")
            print("      pip install rapidocr-onnxruntime\n")
            return False

    _ensure_mss()
    _check_gpu()
    return True


def _ensure_mss():
    global mss_mod
    if mss_mod is None:
        try:
            import mss as _mss
            mss_mod = _mss
            print("  [deps] mss available -> using fast screen capture")
        except ImportError:
            mss_mod = False
            print("  [deps] mss not installed (pip install mss) -> "
                  "falling back to pyautogui.screenshot (slower)")
    return mss_mod


def _check_gpu():
    global GPU_READY
    if not USE_GPU:
        return False
    try:
        count = cv2.cuda.getCudaEnabledDeviceCount()
        if count > 0:
            GPU_READY = True
            print(f"  [gpu] {count} CUDA device(s) detected -> GPU template matching ON")
        else:
            print("  [gpu] no CUDA device found, using CPU.")
    except AttributeError:
        print("  [gpu] this OpenCV build has no cv2.cuda module, using CPU.")
    return GPU_READY


# =========================================================================
# PATHS + CONFIG
# =========================================================================
ASSET_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE   = os.path.join(ASSET_DIR, "config.json")
LOG_FILE      = os.path.join(ASSET_DIR, "bot_log.csv")
LOOT_LOG_FILE = os.path.join(ASSET_DIR, "loot_log.csv")
DEBUG_DIR     = os.path.join(ASSET_DIR, "debug_crops")

# key -> template filename (same file names as before, new images inside)
ASSETS = {
    "attack_menu":      "attack_btn_menu_1.png",   # home "Battle" button
    "find_match":       "find_match_btn_2.png",    # "Find a Match" text (left Battle card)
    "attack_go":        "attack_btn_go_3.png",     # green "Attack!" on the army screen
    "end_battle":       "end_battlel_5.png",       # red "End Battle"
    "surrender_dialog": "surrender_okay_6.png",    # green "Okay" on the surrender dialog
    "return_home":      "return_home_7.png",       # "Return Home" on the result screen (optional)
    "surrender_btn":    "surrender_btn_8.png",     # red "Surrender" button during battle
}
ASSET_PATHS = {k: os.path.join(ASSET_DIR, v) for k, v in ASSETS.items()}

SCHEMA = 3

DEFAULT_SCALES = [0.5, 0.6, 0.7, 0.75, 0.8, 0.85, 0.9, 0.95,
                  1.0, 1.05, 1.1, 1.15, 1.2, 1.3, 1.4, 1.5]

# All positions below are FRACTIONS (0..1) of the game picture, so they keep
# working at any window size. Run menu option 5 once to tell the bot where
# the game picture is inside the window.
CARD_Y = 0.924
DEFAULT_CONFIG = {
    "schema": SCHEMA,
    "confidence": {
        "attack_menu":      0.80,
        "find_match":       0.85,
        "attack_go":        0.82,
        "end_battle":       0.80,
        "surrender_dialog": 0.85,
        "return_home":      0.82,
        "surrender_btn":    0.80,
    },
    "scales": DEFAULT_SCALES,
    "preferred_scale": {},
    # Search area limits (x0, y0, x1, y1 as fractions of the game picture).
    # find_match is limited to the LEFT card so it can never click "Ranked Battle".
    "roi": {"find_match": [0.0, 0.30, 0.27, 0.85]},
    "viewport": None,
    "storage": {
        "max_gold": 0,        # your total gold capacity   (0 = bot asks you)
        "max_elixir": 0,      # your total elixir capacity (0 = bot asks you)
        "max_dark": 0,        # 0 = ignore dark elixir
        "stop_when": "all",   # "all" = every listed storage full, "any" = first one full
        "full_ratio": 0.99,
    },
    # Where the resource numbers are on the HOME screen (x, y, w, h fractions)
    "regions": {
        "gold":        [0.8598, 0.0232, 0.1100, 0.0435],
        "elixir":      [0.8598, 0.0813, 0.1100, 0.0435],
        "dark_elixir": [0.8761, 0.1393, 0.0937, 0.0406],
    },
    "deploy": {
        "use_hotkeys": True,      # key press (hardware scancodes). False = click the cards
        "click_card_fallback": False,  # True = ALSO click the card after pressing its key
        "zoom_out_steps": 0,      # Ctrl+minus presses before deploying (0 = off)
        "tap_delay": 0.012,       # (old, unused - see verify.tap_delay)
        "select_delay": 0.04,     # seconds after selecting a card
        # v4: the bot READS the card bar. A card that is still coloured = still has
        # units, a grey card = empty (exactly like your screenshot). It keeps tapping
        # until every card is grey, then runs sweeps for anything that was missed.
        "verify": {
            "enabled": True,
            "tap_delay": 0.004,   # pause between taps (lower = faster)
            "batch": 10,          # troops: taps between card checks (first part)
            "free_frac": 0.7,     # troops: share of `taps` sent before checking every tap
            "settle": 0.03,       # wait before reading the card after a tap
            "max_extra": 1.6,     # safety cap = taps * this (+10)
            "sweep_passes": 4,    # re-check passes after everything is deployed
            "sat_min": 45,        # HSV saturation of a "coloured" pixel
            "colored_frac": 0.10, # share of coloured pixels = card still has units
            "patch_w": 0.030,     # sampled patch around each card centre (fractions)
            "patch_h": 0.055,
        },
        # The RED-LINE diamond of the base, as fractions of the game picture.
        # Troops go on a ring just OUTSIDE it. Use menu option 6 to check/tune.
        "base": {"cx": 0.478, "cy": 0.447, "hw": 0.341, "hh": 0.370},
        "ring_margin": 1.10,      # 1.00 = on the diamond; >1 = further outside
        "ring_spread": 0.04,      # extra random outward distance per tap
        "edge_trim": 0.12,        # keep away from the diamond corners (0..0.5)
        # Screen areas never tapped (x0, y0, x1, y1): UI buttons, timer, loot, card bar
        "avoid": [[0.0, 0.0, 0.26, 0.22], [0.36, 0.0, 0.64, 0.10],
                  [0.78, 0.0, 1.0, 0.20], [0.0, 0.70, 0.42, 1.0],
                  [0.0, 0.82, 1.0, 1.0], [0.82, 0.70, 1.0, 1.0]],
        # YOUR ARMY. Edit this list when the army changes - keys are the ones
        # printed on the cards (1.. troops, z.. siege, q.. heroes, a.. spells).
        # kind: "troop" / "siege" / "hero" = ring deploy, "spell" = on the base.
        # taps: troops = number of units (1 tap = 1 unit), spells = how many.
        # delay: seconds after the last troop before this spell is cast.
        "units": [
            {"name": "siege",   "kind": "siege", "key": "z", "taps": 1},
            {"name": "goblins", "kind": "troop", "key": "1", "taps": 117},
            {"name": "hero1",   "kind": "hero",  "key": "q", "taps": 1},
            {"name": "hero2",   "kind": "hero",  "key": "w", "taps": 1},
            {"name": "hero3",   "kind": "hero",  "key": "e", "taps": 1},
            {"name": "hero4",   "kind": "hero",  "key": "r", "taps": 1},
            {"name": "spell_a", "kind": "spell", "key": "a", "taps": 11, "delay": 1.5},
        ],
        # Card x positions (fraction) used only when use_hotkeys is False / fallback.
        "cards": {"1": 0.171, "z": 0.246, "q": 0.314, "w": 0.368, "e": 0.436,
                  "r": 0.500, "a": 0.571, "s": 0.641, "d": 0.706, "f": 0.772},
        "card_y": 0.905,
        # Hero abilities: press each hero key again this many seconds after deploying
        "abilities": {"after_seconds": 2.5, "keys": ["q", "w", "e", "r"]},
    },
    "battle": {
        "timeout": 200,       # seconds to wait for the battle to end by itself
        "manual_wait": 60,    # mode 3: seconds you get to deploy manually
        "return_home_pos": [0.5, 0.88],  # used only if return_home_7.png is missing
        "cooldown": 0,        # seconds to pause between attacks (e.g. army training time)
    },
}


def _merge(defaults, user):
    """Recursive merge: user values win, missing keys come from defaults."""
    out = copy.deepcopy(defaults)
    if not isinstance(user, dict):
        return out
    for k, v in user.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config():
    user = {}
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                user = json.load(f)
        except Exception as e:
            print(f"  ! could not read config.json ({e}), using defaults")
    if user.get("schema") != SCHEMA:
        # config from the old UI: its thresholds / scale memory no longer apply
        keep_vp = user.get("viewport")
        user = {"viewport": keep_vp} if keep_vp else {}
        print("  [config] old config detected -> reset to new-UI defaults")
    cfg = _merge(DEFAULT_CONFIG, user)
    cfg["schema"] = SCHEMA
    save_config(cfg)
    return cfg


def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)


# =========================================================================
# CONSTANTS
# =========================================================================
# Window title must CONTAIN this (case-insensitive). Keep it short.
WINDOW_TITLE_KEYWORD = "Clash of Clans"
_BAD_TITLE_PARTS = ("python", ".py", "powershell", "cmd.exe", "command prompt",
                    "visual studio", "vs code", "notepad", "terminal", "chrome",
                    "edge", "firefox")

MATCHMAKING_TIMEOUT = 90
STEP_TIMEOUT        = 12
SCAN_INTERVAL       = 0.05
CLICK_MOVE_DURATION = 0.02
POST_CLICK_PAUSE    = 0.4
BASE_PREVIEW_WAIT   = 0.3
FALLBACK_MARGIN     = 0.05
ZOOM_STEP_DELAY     = 0.3

SANITY_MAX = 200_000_000
SAVE_DEBUG = False


# =========================================================================
# MENU
# =========================================================================
def print_menu(cfg=None):
    print()
    print("=" * 60)
    print("  Clash of Clans Auto-Attack Bot  (v2 - new UI)")
    print("=" * 60)
    print("  1) Full auto")
    print("     attacks forever, auto-deploys the army listed in config.json")
    print()
    print("  2) Full auto + storage check")
    print("     same as 1, but stops when your storages are full")
    print()
    print("  3) Auto attack only (NO deploy)")
    print("     clicks through the menus, you deploy manually")
    print()
    print("  4) Test / calibrate asset detection (no clicks)")
    print()
    print("  5) Calibrate game area            <-- do this first")
    print("  6) Preview deploy points + test hotkeys (no attack)")
    print()
    print("  0) Exit")
    print("=" * 60)
    vp = (cfg or {}).get("viewport")
    print(f"  game area: {'calibrated' if vp else 'NOT calibrated (run option 5)'}"
          f"   gpu={'ON' if (USE_GPU and GPU_READY) else 'off'}"
          f"   capture={'mss' if mss_mod else 'pyautogui'}")


# =========================================================================
# LOGGING
# =========================================================================
def ensure_log_header():
    if not os.path.exists(LOG_FILE):
        with open(LOG_FILE, "w", newline="") as f:
            csv.writer(f).writerow([
                "timestamp", "step", "asset", "found", "score", "threshold",
                "scale", "rel_x", "rel_y", "abs_x", "abs_y",
                "window_left", "window_top", "window_w", "window_h", "note",
            ])


def log_attempt(step, asset_key, match, region, note=""):
    ts = datetime.now().isoformat()
    if "error" in match:
        row = [ts, step, asset_key, False, "", "", "", "", "", "", "",
               region[0], region[1], region[2], region[3], match["error"]]
        print(f"[log] {step:<26} | {asset_key:<16} | ERROR: {match['error']}")
    else:
        cx, cy = match["x"] + match["w"] // 2, match["y"] + match["h"] // 2
        abs_x, abs_y = region[0] + cx, region[1] + cy
        row = [ts, step, asset_key, match["found"], f"{match['score']:.4f}",
               f"{match['threshold']:.2f}", f"{match['scale']:.2f}",
               cx, cy, abs_x, abs_y,
               region[0], region[1], region[2], region[3], note]
        status = "FOUND" if match["found"] else "miss "
        print(f"[log] {step:<26} | {asset_key:<16} | {status} | "
              f"score={match['score']:.3f} (need {match['threshold']:.2f}) | "
              f"scale={match['scale']:.2f} | abs=({abs_x},{abs_y})")
    with open(LOG_FILE, "a", newline="") as f:
        csv.writer(f).writerow(row)


def log_loot_row(before, after, gained):
    exists = os.path.exists(LOOT_LOG_FILE)
    keys = ("gold", "elixir", "dark_elixir")

    def v(d, k):
        val = d.get(k)
        return "" if val is None else val

    with open(LOOT_LOG_FILE, "a", newline="") as f:
        w = csv.writer(f)
        if not exists:
            w.writerow(["timestamp"]
                       + [f"{k}_before" for k in keys]
                       + [f"{k}_after" for k in keys]
                       + [f"{k}_gained" for k in keys])
        w.writerow([datetime.now().isoformat()]
                   + [v(before, k) for k in keys]
                   + [v(after, k) for k in keys]
                   + [v(gained, k) for k in keys])


def print_resources(tag, res):
    if not res:
        print(f"[loot] {tag}  (read failed)")
        return

    def fmt(v):
        return f"{v:>11,}" if v is not None else "        n/a"

    print(f"[loot] {tag}  gold={fmt(res.get('gold'))}  "
          f"elixir={fmt(res.get('elixir'))}  de={fmt(res.get('dark_elixir'))}")


# =========================================================================
# WINDOW + CAPTURE
# =========================================================================
def find_game_window():
    kw = WINDOW_TITLE_KEYWORD.lower()
    cands = []
    for w in gw.getAllWindows():
        t = w.title.lower()
        if kw not in t or w.width < 200 or w.height < 200:
            continue
        if any(b in t for b in _BAD_TITLE_PARTS):
            continue
        cands.append(w)
    if not cands:
        return None
    cands.sort(key=lambda w: (not w.title.lower().startswith(kw), -(w.width * w.height)))
    return cands[0]


def ensure_game_running():
    """Waits for the game window. It does NOT launch anything - open
    Clash of Clans from the Google Play Games app yourself."""
    win = find_game_window()
    if win:
        return win

    print(f"No window with '{WINDOW_TITLE_KEYWORD}' in its title found.")
    print("Open Clash of Clans from the Google Play Games app and wait for the")
    print("village to load. Waiting up to 120s...")
    for _ in range(120):
        time.sleep(1)
        win = find_game_window()
        if win:
            print(f"Game window detected: {win.title!r}")
            return win

    print("\nWindows currently open (copy the right keyword into WINDOW_TITLE_KEYWORD):")
    for w in gw.getAllWindows():
        if w.title.strip():
            print(f"   {w.title!r}")
    raise RuntimeError("Game window not found.")


_ACTIVATE_EVERY = 2.0
_last_activate_ts = 0.0


def get_region(win, force_activate=False):
    """(left, top, width, height) of the game window. Re-focuses it every 2s."""
    global _last_activate_ts
    now = time.time()
    try:
        if win.isMinimized:
            win.restore()
            time.sleep(0.3)
            force_activate = True
        if force_activate or (now - _last_activate_ts) >= _ACTIVATE_EVERY:
            win.activate()
            _last_activate_ts = now
    except Exception:
        pass  # pygetwindow raises a bogus error even when activate() worked
    return (win.left, win.top, win.width, win.height)


def park_mouse():
    """Move the mouse to the screen centre so a mouse resting in a corner
    doesn't trip pyautogui's fail-safe on the first action."""
    try:
        w, h = pyautogui.size()
        pyautogui.FAILSAFE = False
        pyautogui.moveTo(w // 2, h // 2)
    finally:
        pyautogui.FAILSAFE = True


_mss_cache = {}


def capture(region):
    left, top, width, height = region
    if mss_mod:
        sct = _mss_cache.get("sct")
        if sct is None:
            sct = mss_mod.mss()
            _mss_cache["sct"] = sct
        raw = sct.grab({"left": left, "top": top, "width": width, "height": height})
        return np.array(raw)[:, :, :3].copy()
    shot = pyautogui.screenshot(region=region)
    return cv2.cvtColor(np.array(shot), cv2.COLOR_RGB2BGR)


def vp_in_window(cfg):
    """Game picture rect (dx, dy, w, h) in WINDOW coordinates, or None."""
    vp = cfg.get("viewport")
    if not vp:
        return None
    return (vp["dx"], vp["dy"], vp["w"], vp["h"])


def grab(win, cfg):
    """-> (region, screen_bgr, vp). vp is the game picture inside `screen`."""
    region = get_region(win)
    screen = capture(region)
    return region, screen, vp_in_window(cfg)


def vp_abs(win, cfg, fx, fy):
    """Fractions of the game picture -> absolute screen pixels."""
    vp = cfg.get("viewport")
    if vp:
        x = win.left + vp["dx"] + fx * vp["w"]
        y = win.top + vp["dy"] + fy * vp["h"]
    else:
        x = win.left + fx * win.width
        y = win.top + fy * win.height
    sw, sh = pyautogui.size()
    return min(max(x, 2), sw - 3), min(max(y, 2), sh - 3)


def click_region_point(region, rel_x, rel_y):
    abs_x = region[0] + rel_x
    abs_y = region[1] + rel_y
    sw, sh = pyautogui.size()
    safe_x = min(max(abs_x, 2), sw - 3)
    safe_y = min(max(abs_y, 2), sh - 3)
    pyautogui.moveTo(safe_x, safe_y, duration=CLICK_MOVE_DURATION)
    pyautogui.click()


# =========================================================================
# TEMPLATE MATCHING
# =========================================================================
_TEMPLATE_CACHE = {}
_GPU_TEMPLATE_CACHE = {}
_gpu_screen_mat = None


def _load_template(asset_key):
    if asset_key not in _TEMPLATE_CACHE:
        path = ASSET_PATHS[asset_key]
        if not os.path.exists(path):
            _TEMPLATE_CACHE[asset_key] = None
            return None, f"missing asset: {path}"
        img = cv2.imread(path)
        if img is None:
            _TEMPLATE_CACHE[asset_key] = None
            return None, f"could not read: {path}"
        _TEMPLATE_CACHE[asset_key] = img
    img = _TEMPLATE_CACHE[asset_key]
    if img is None:
        return None, f"missing/unreadable asset: {asset_key}"
    return img, None


def match_template(screen_bgr, asset_key, cfg, thorough=False, vp=None):
    """Find asset_key in screen_bgr over several scales.

    thorough=False stops at the first scale that clears the threshold
    (fast; the result is still `found`). thorough=True scans every scale so
    the reported score is the true best (used by the test mode).
    vp = game picture rect inside screen_bgr; used for per-asset search areas.
    """
    template_orig, err = _load_template(asset_key)
    if err:
        return {"error": err}

    threshold = cfg["confidence"].get(asset_key, 0.82)

    img, ox, oy = screen_bgr, 0, 0
    roi = cfg.get("roi", {}).get(asset_key)
    if roi and vp:
        sh, sw = screen_bgr.shape[:2]
        x0 = int(max(0, vp[0] + roi[0] * vp[2]))
        y0 = int(max(0, vp[1] + roi[1] * vp[3]))
        x1 = int(min(sw, vp[0] + roi[2] * vp[2]))
        y1 = int(min(sh, vp[1] + roi[3] * vp[3]))
        if x1 - x0 > 20 and y1 - y0 > 20:
            img = screen_bgr[y0:y1, x0:x1]
            ox, oy = x0, y0

    screen_h, screen_w = img.shape[:2]

    scales = list(cfg["scales"])
    pref = cfg.get("preferred_scale", {}).get(asset_key)
    if pref is not None and pref in scales:
        scales.remove(pref)
        scales.insert(0, pref)

    stop_early = not thorough
    if GPU_READY and USE_GPU:
        best = _match_template_gpu(img, template_orig, asset_key, scales,
                                   screen_w, screen_h, threshold, stop_early)
    else:
        best = _match_template_cpu(img, template_orig, scales,
                                   screen_w, screen_h, threshold, stop_early)

    if best is None:
        return {"error": "template larger than the search area at every scale"}

    best["x"] += ox
    best["y"] += oy
    best["threshold"] = threshold
    best["found"] = best["score"] >= threshold
    return best


def _match_template_cpu(screen_bgr, template_orig, scales, screen_w, screen_h,
                        threshold, stop_early):
    best = None
    h0, w0 = template_orig.shape[:2]
    for scale in scales:
        w, h = int(w0 * scale), int(h0 * scale)
        if w < 5 or h < 5 or w > screen_w or h > screen_h:
            continue
        template = cv2.resize(template_orig, (w, h), interpolation=cv2.INTER_AREA)
        result = cv2.matchTemplate(screen_bgr, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, max_loc = cv2.minMaxLoc(result)
        if best is None or max_val > best["score"]:
            best = {"x": max_loc[0], "y": max_loc[1], "w": w, "h": h,
                    "score": max_val, "scale": scale}
        if stop_early and max_val >= threshold:
            break
    return best


def _match_template_gpu(screen_bgr, template_orig, asset_key, scales,
                        screen_w, screen_h, threshold, stop_early):
    global _gpu_screen_mat
    try:
        if _gpu_screen_mat is None:
            _gpu_screen_mat = cv2.cuda_GpuMat()
        _gpu_screen_mat.upload(screen_bgr)

        h0, w0 = template_orig.shape[:2]
        best = None
        for scale in scales:
            w, h = int(w0 * scale), int(h0 * scale)
            if w < 5 or h < 5 or w > screen_w or h > screen_h:
                continue
            cache_key = (asset_key, scale)
            tmpl_gpu = _GPU_TEMPLATE_CACHE.get(cache_key)
            if tmpl_gpu is None:
                resized = cv2.resize(template_orig, (w, h), interpolation=cv2.INTER_AREA)
                tmpl_gpu = cv2.cuda_GpuMat()
                tmpl_gpu.upload(resized)
                _GPU_TEMPLATE_CACHE[cache_key] = tmpl_gpu
            matcher = cv2.cuda.createTemplateMatching(cv2.CV_8UC3, cv2.TM_CCOEFF_NORMED)
            result = matcher.match(_gpu_screen_mat, tmpl_gpu).download()
            _, max_val, _, max_loc = cv2.minMaxLoc(result)
            if best is None or max_val > best["score"]:
                best = {"x": max_loc[0], "y": max_loc[1], "w": w, "h": h,
                        "score": max_val, "scale": scale}
            if stop_early and max_val >= threshold:
                break
        return best
    except Exception as e:
        print(f"  [gpu] match failed ({e}), CPU fallback for this frame")
        return _match_template_cpu(screen_bgr, template_orig, scales,
                                   screen_w, screen_h, threshold, stop_early)


def center_of(match):
    return match["x"] + match["w"] // 2, match["y"] + match["h"] // 2


def wait_and_click(win, cfg, asset_key, label, timeout=STEP_TIMEOUT):
    print(f"Looking for '{label}'...")
    start = time.time()
    attempt = 0
    best = None
    region = get_region(win)

    while time.time() - start < timeout:
        attempt += 1
        region, screen, vp = grab(win, cfg)
        match = match_template(screen, asset_key, cfg, vp=vp)

        if "error" in match:
            print(f"  [{attempt}] {asset_key}: {match['error']}")
        else:
            if attempt == 1 or attempt % 10 == 0:
                status = "FOUND" if match["found"] else "miss"
                print(f"  [{attempt}] {asset_key}: {status} score={match['score']:.3f} "
                      f"(need {match['threshold']:.2f}) scale={match['scale']:.2f}")
            if best is None or match["score"] > best["score"]:
                best = match
            if match["found"]:
                log_attempt(label, asset_key, match, region)
                cfg.setdefault("preferred_scale", {})[asset_key] = match["scale"]
                save_config(cfg)
                cx, cy = center_of(match)
                click_region_point(region, cx, cy)
                return True
        time.sleep(SCAN_INTERVAL)

    if best is not None and best["score"] >= best["threshold"] - FALLBACK_MARGIN:
        print(f"  -> fallback click (score {best['score']:.3f})")
        log_attempt(label, asset_key, best, region, note="FALLBACK_CLICK")
        cx, cy = center_of(best)
        click_region_point(region, cx, cy)
        return True

    log_attempt(label, asset_key, best or {"error": "no attempt"}, region, note="TIMEOUT")
    return False


def wait_for_any(win, cfg, keys, timeout):
    """Polls until one of `keys` is on screen. -> (key, match, region) or (None, None, None)."""
    start = time.time()
    while time.time() - start < timeout:
        region, screen, vp = grab(win, cfg)
        for k in keys:
            m = match_template(screen, k, cfg, vp=vp)
            if "error" not in m and m["found"]:
                return k, m, region
        time.sleep(SCAN_INTERVAL)
    return None, None, None


# =========================================================================
# OCR (storage check)
# =========================================================================
def _get_ocr():
    global _ocr_engine
    if _ocr_engine is None:
        _ocr_engine = RapidOCR()
    return _ocr_engine


def _preprocess(crop_bgr):
    """Keep only near-white pixels (the number text) so the coloured storage
    bar behind it - bright yellow/pink when the storage is full - is ignored."""
    m = crop_bgr.min(axis=2).astype(np.float32)
    m = np.clip((m - 90) / (230 - 90), 0, 1) * 255
    m = m.astype(np.uint8)
    m = cv2.resize(m, None, fx=3, fy=3, interpolation=cv2.INTER_CUBIC)
    m = cv2.GaussianBlur(m, (3, 3), 0)
    m = 255 - m
    # white margin: the OCR detector drops digits that touch the image border
    m = cv2.copyMakeBorder(m, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=255)
    return cv2.cvtColor(m, cv2.COLOR_GRAY2BGR)


def _parse_number(text):
    digits = re.sub(r"[^\d]", "", text or "")
    return int(digits) if digits else None


def read_resources(win, cfg, verbose=True):
    """Reads gold / elixir / dark elixir from the HOME screen."""
    region, screen, vp = grab(win, cfg)
    if vp is None:
        vp = (0, 0, region[2], region[3])
    engine = _get_ocr()
    out = {}

    if SAVE_DEBUG:
        os.makedirs(DEBUG_DIR, exist_ok=True)
        cv2.imwrite(os.path.join(DEBUG_DIR, "_window.png"), screen)

    for name, (fx, fy, fw, fh) in cfg["regions"].items():
        x0 = int(vp[0] + fx * vp[2])
        y0 = int(vp[1] + fy * vp[3])
        x1 = int(x0 + fw * vp[2])
        y1 = int(y0 + fh * vp[3])
        crop = screen[max(0, y0):y1, max(0, x0):x1]
        if crop.size == 0:
            out[name] = None
            continue
        if SAVE_DEBUG:
            cv2.imwrite(os.path.join(DEBUG_DIR, f"{name}.png"), crop)
        try:
            result, _ = engine(_preprocess(crop))
        except Exception:
            out[name] = None
            continue
        if not result:
            out[name] = None
            if verbose:
                print(f"    [{name}] raw='' -> None")
            continue
        text = " ".join(item[1] for item in result)
        value = _parse_number(text)
        if value is not None and value > SANITY_MAX:
            if verbose:
                print(f"    [{name}] raw={text!r} -> {value}  ** ignored **")
            value = None
        elif verbose:
            print(f"    [{name}] raw={text!r} -> {value}")
        out[name] = value
    return out


def is_storage_full(res, cfg):
    if not res:
        return False
    s = cfg["storage"]
    checks = []
    for key, cap_key in (("gold", "max_gold"), ("elixir", "max_elixir"),
                         ("dark_elixir", "max_dark")):
        cap = s.get(cap_key) or 0
        if cap <= 0:
            continue
        v = res.get(key)
        checks.append(v is not None and v >= cap * s["full_ratio"])
    if not checks:
        return False
    return all(checks) if s["stop_when"] == "all" else any(checks)


def ensure_storage_caps(cfg):
    s = cfg["storage"]
    if s["max_gold"] > 0 and s["max_elixir"] > 0:
        return
    print("\nThe bot needs your TOTAL storage capacity to know when you are full.")
    print("(Add up all your Gold Storages / Elixir Storages + Town Hall. Numbers only.)")

    def ask(prompt, required):
        while True:
            raw = input(prompt).strip()
            digits = re.sub(r"\D", "", raw)
            if digits:
                return int(digits)
            if not required:
                return 0
            print("  please type a number, e.g. 24000000")

    s["max_gold"] = ask("  Max gold capacity   : ", True)
    s["max_elixir"] = ask("  Max elixir capacity : ", True)
    s["max_dark"] = ask("  Max dark elixir capacity (Enter to ignore): ", False)
    save_config(cfg)
    print("  saved to config.json\n")


# =========================================================================
# INPUT HELPERS + DEPLOY
# =========================================================================
# Hardware scancodes: many games / Google Play Games ignore the virtual-key
# events pyautogui sends, which is why hotkeys "did nothing".
_SCANCODES = {
    "1": 0x02, "2": 0x03, "3": 0x04, "4": 0x05, "5": 0x06, "6": 0x07,
    "q": 0x10, "w": 0x11, "e": 0x12, "r": 0x13, "t": 0x14,
    "a": 0x1E, "s": 0x1F, "d": 0x20, "f": 0x21, "g": 0x22,
    "z": 0x2C, "x": 0x2D, "c": 0x2E, "v": 0x2F,
}


def _send_scancode(sc, up):
    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong))]

    class _U(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("u", _U)]

    flags = 0x0008 | (0x0002 if up else 0)          # SCANCODE | KEYUP
    inp = INPUT(type=1, u=_U(ki=KEYBDINPUT(0, sc, flags, 0, None)))
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(inp))


def press_key(key, hold=0.05):
    key = key.lower()
    if os.name == "nt" and key in _SCANCODES:
        _send_scancode(_SCANCODES[key], False)
        time.sleep(hold)
        _send_scancode(_SCANCODES[key], True)
    else:
        pyautogui.keyDown(key)
        time.sleep(hold)
        pyautogui.keyUp(key)


_U32 = None
_tap_count = 0


def _fast_start():
    """Windows: 1 ms timer + direct SendInput-style clicks (no pyautogui overhead)."""
    global _U32
    _U32 = None
    if os.name != "nt":
        return
    try:
        import ctypes
        _U32 = ctypes.windll.user32
        ctypes.windll.winmm.timeBeginPeriod(1)
    except Exception:
        _U32 = None


def _fast_stop():
    global _U32
    if _U32 is not None:
        try:
            import ctypes
            ctypes.windll.winmm.timeEndPeriod(1)
        except Exception:
            pass
    _U32 = None


def _spin(sec):
    """Accurate short sleep (busy-wait for the last 2 ms)."""
    if sec <= 0:
        return
    end = time.perf_counter() + sec
    if sec > 0.004:
        time.sleep(sec - 0.003)
    while time.perf_counter() < end:
        pass


def click_abs(x, y):
    global _tap_count
    if _U32 is not None:
        _tap_count += 1
        if _tap_count % 8 == 0:
            pyautogui.failSafeCheck()          # keep the corner emergency stop alive
        _U32.SetCursorPos(int(x), int(y))
        _spin(0.002)
        _U32.mouse_event(0x0002, 0, 0, 0, 0)   # left down
        _spin(0.003)
        _U32.mouse_event(0x0004, 0, 0, 0, 0)   # left up
        return
    pyautogui.moveTo(x, y)
    time.sleep(0.004)
    pyautogui.mouseDown()
    time.sleep(0.008)
    pyautogui.mouseUp()


def tap_frac(win, cfg, fx, fy, jitter=0.0):
    get_region(win)
    if jitter:
        fx += random.uniform(-jitter, jitter)
        fy += random.uniform(-jitter, jitter)
    x, y = vp_abs(win, cfg, fx, fy)
    click_abs(x, y)


def select_card(win, cfg, key):
    d = cfg["deploy"]
    if d["use_hotkeys"] and key:
        press_key(key)
        if d.get("click_card_fallback") and key in d["cards"]:
            time.sleep(0.03)
            tap_frac(win, cfg, d["cards"][key], d["card_y"])
    elif key in d["cards"]:
        tap_frac(win, cfg, d["cards"][key], d["card_y"])
    time.sleep(d["select_delay"])


def zoom_out_and_center(win, cfg):
    steps = cfg["deploy"]["zoom_out_steps"]
    if steps <= 0:
        return
    print(f"Zooming out ({steps} steps)...")
    x, y = vp_abs(win, cfg, 0.5, 0.5)
    pyautogui.moveTo(x, y, duration=0.1)
    time.sleep(0.15)
    for _ in range(steps):
        pyautogui.keyDown("ctrl")
        pyautogui.press("-")
        pyautogui.keyUp("ctrl")
        time.sleep(ZOOM_STEP_DELAY)


# ---- deploy geometry ------------------------------------------------------
def _in_avoid(d, fx, fy):
    for x0, y0, x1, y1 in d["avoid"]:
        if x0 <= fx <= x1 and y0 <= fy <= y1:
            return True
    return False


def _diamond(d, scale):
    b = d["base"]
    cx, cy, hw, hh = b["cx"], b["cy"], b["hw"] * scale, b["hh"] * scale
    # corners: left, top, right, bottom -> 4 edges = the 4 sides of the base
    L, T, R, B = (cx - hw, cy), (cx, cy - hh), (cx + hw, cy), (cx, cy + hh)
    return {"NW": (L, T), "NE": (T, R), "SE": (R, B), "SW": (B, L)}


def ring_point(d, side):
    """A fresh random point on `side`, just outside the red line. None if the
    side has no usable spot (e.g. it is covered by UI)."""
    trim = d["edge_trim"]
    for _ in range(40):
        scale = d["ring_margin"] + random.uniform(0, d["ring_spread"])
        (x0, y0), (x1, y1) = _diamond(d, scale)[side]
        t = random.uniform(trim, 1 - trim)
        fx, fy = x0 + (x1 - x0) * t, y0 + (y1 - y0) * t
        if 0.01 < fx < 0.99 and 0.01 < fy < 0.99 and not _in_avoid(d, fx, fy):
            return fx, fy
    return None


def base_point(d):
    """Random point anywhere on the base (spells)."""
    b = d["base"]
    for _ in range(40):
        u, v = random.uniform(-1, 1), random.uniform(-1, 1)
        if abs(u) + abs(v) > 0.85:          # stay inside the diamond
            continue
        fx, fy = b["cx"] + u * b["hw"], b["cy"] + v * b["hh"]
        if not _in_avoid(d, fx, fy):
            return fx, fy
    return b["cx"], b["cy"]


def _usable_sides(d):
    return [s for s in ("NW", "NE", "SE", "SW") if ring_point(d, s) is not None]


def read_cards(win, cfg, keys):
    """Looks at the card bar. -> {key: (has_units, coloured_fraction)}.
    A card with units is coloured, an empty one is grey (x0)."""
    d = cfg["deploy"]
    v = d["verify"]
    get_region(win)
    cy, ph, pw = d["card_y"], v["patch_h"], v["patch_w"]
    x0, y0 = vp_abs(win, cfg, 0.0, cy - ph / 2 - 0.02)
    x1, y1 = vp_abs(win, cfg, 1.0, cy + ph / 2 + 0.02)
    left, top = int(x0), int(y0)
    w, h = max(1, int(x1 - x0)), max(1, int(y1 - y0))
    hsv = cv2.cvtColor(capture((left, top, w, h)), cv2.COLOR_BGR2HSV)
    out = {}
    for k in keys:
        fx = d["cards"].get(k)
        if fx is None:
            continue
        ax0, ay0 = vp_abs(win, cfg, fx - pw / 2, cy - ph / 2)
        ax1, ay1 = vp_abs(win, cfg, fx + pw / 2, cy + ph / 2)
        px0, px1 = max(0, int(ax0) - left), min(w, int(ax1) - left)
        py0, py1 = max(0, int(ay0) - top), min(h, int(ay1) - top)
        if px1 <= px0 or py1 <= py0:
            continue
        frac = float((hsv[py0:py1, px0:px1, 1] > v["sat_min"]).mean())
        out[k] = (frac > v["colored_frac"], frac)
    return out


def deploy_unit(win, cfg, unit, abs_fn, verify=True):
    """Deploys one card. With verify on, it taps until the card is grey (empty)."""
    d = cfg["deploy"]
    v = d["verify"]
    kind = unit.get("kind", "troop")
    key = unit.get("key")
    name = unit.get("name", key)
    hint = max(1, int(unit.get("taps", 1)))
    use_v = bool(verify and v.get("enabled", True) and key in d["cards"])

    def has_units():
        st = read_cards(win, cfg, [key]).get(key)
        return True if st is None else st[0]

    if use_v and not has_units():
        print(f"  {name} [{key}] empty - skipped")
        return 0

    t_start = time.time()
    select_card(win, cfg, key)

    sides = None
    if kind != "spell":
        sides = _usable_sides(d)
        if not sides:
            print("    ! no usable deploy spot - check deploy.base / ring_margin (option 6)")
            return 0

    def next_point(n):
        if kind == "spell":
            return base_point(d)
        for j in range(len(sides)):
            p = ring_point(d, sides[((n // 4) + j) % len(sides)])
            if p:
                return p
        return None

    delay = v["tap_delay"] * (2 if kind == "spell" else 1)
    n = 0
    if not use_v:                                   # blind mode (old behaviour)
        while n < hint:
            p = next_point(n)
            if p is None:
                break
            x, y = abs_fn(*p)
            click_abs(x, y)
            n += 1
            _spin(delay)
        print(f"  {unit['name']} [{key}] x{n} (blind)")
        return n

    cap = hint + max(10, int(hint * (v["max_extra"] - 1)))
    free = int(hint * v["free_frac"]) if kind == "troop" else 0
    empty = False
    while n < cap:
        burst = min(v["batch"], free - n) if n < free else 1
        done_now = 0
        for _ in range(burst):
            p = next_point(n)
            if p is None:
                break
            x, y = abs_fn(*p)
            click_abs(x, y)
            n += 1
            done_now += 1
            _spin(delay)
        if done_now == 0:
            break
        if n >= free:
            _spin(v["settle"])
            if not has_units():
                empty = True
                break
    state = "EMPTY" if empty else "cap reached, card still coloured"
    print(f"  {name} [{key}] {n} taps in {time.time() - t_start:.2f}s -> {state}")
    return n


def deploy_army(win, cfg):
    d = cfg["deploy"]
    v = d["verify"]
    print("\n=== Deploying ===")
    get_region(win, force_activate=True)
    zoom_out_and_center(win, cfg)

    get_region(win)
    vp = cfg.get("viewport")
    sw, sh = pyautogui.size()

    def abs_fn(fx, fy):
        if vp:
            x = win.left + vp["dx"] + fx * vp["w"]
            y = win.top + vp["dy"] + fy * vp["h"]
        else:
            x, y = win.left + fx * win.width, win.top + fy * win.height
        return min(max(x, 2), sw - 3), min(max(y, 2), sh - 3)

    units = [u for u in d["units"] if u.get("key")]
    keys = [u["key"] for u in units]
    verify = bool(v.get("enabled", True))
    if verify:
        st = read_cards(win, cfg, keys)
        if not st or not any(c for c, _ in st.values()):
            print("  ! the card bar reads as completely grey/unknown -> check deploy.cards /"
                  " card_y with menu option 6.")
            print("    Verification is OFF for this attack (blind taps).")
            verify = False
        else:
            print("  card bar: " + "  ".join(
                f"{k.upper()}={'army' if c else 'empty'}" for k, (c, _) in st.items()))
            absent = {k for k, (c, _) in st.items() if not c}
            units = [u for u in units if u["key"] not in absent]

    _fast_start()
    try:
        t0 = time.time()
        spells = [u for u in units if u.get("kind") == "spell"]
        others = [u for u in units if u.get("kind") != "spell"]
        for unit in others:                          # all troops first, right away
            deploy_unit(win, cfg, unit, abs_fn, verify)
        t_troops = time.time()

        events = [(t_troops + u.get("delay", 1.5), "spell", u) for u in spells]
        ab = d.get("abilities", {})
        if ab.get("keys"):
            events.append((t0 + ab.get("after_seconds", 6.0), "abilities", ab))
        events.sort(key=lambda e: e[0])
        for when, kind, payload in events:
            wait = when - time.time()
            if wait > 0:
                time.sleep(wait)
            if kind == "abilities":
                print("  hero abilities")
                for key in payload["keys"]:
                    select_card(win, cfg, key)
            else:
                deploy_unit(win, cfg, payload, abs_fn, verify)

        if verify:                                   # sweeps: nothing may stay on the bar
            for i in range(int(v["sweep_passes"])):
                time.sleep(0.12)
                st = read_cards(win, cfg, [u["key"] for u in units])
                left = [u for u in units if st.get(u["key"], (False, 0))[0]]
                if not left:
                    break
                print(f"  sweep {i + 1}: still units on {[u['key'].upper() for u in left]}")
                for u in left:
                    deploy_unit(win, cfg, u, abs_fn, verify)
            time.sleep(0.12)
            st = read_cards(win, cfg, [u["key"] for u in units])
            left = [k.upper() for k, (c, _) in st.items() if c]
            if left:
                print(f"  ! cards NOT empty: {left} (check card positions with option 6)")
            else:
                print("  OK - every card is empty, nothing left to deploy")
    finally:
        _fast_stop()
    print("=== Deploy complete ===\n")


# =========================================================================
# BATTLE FLOW
# =========================================================================
def wait_battle_end(win, cfg, timeout):
    """Polls for the 'Return Home' result screen. -> 'finished' or 'timeout'."""
    start = time.time()
    last_print = 0
    have_rh = _load_template("return_home")[0] is not None
    gone = 0
    while time.time() - start < timeout:
        region, screen, vp = grab(win, cfg)
        if have_rh:
            m = match_template(screen, "return_home", cfg, vp=vp)
            if "error" not in m and m["found"]:
                print("Battle finished.")
                return "finished"
        elif time.time() - start > 6:
            # no Return Home image: battle is over once the Surrender button is gone
            m = match_template(screen, "surrender_btn", cfg, vp=vp)
            gone = gone + 1 if ("error" not in m and not m["found"]) else 0
            if gone >= 3:
                print("Battle finished (Surrender button gone).")
                return "finished"
        el = int(time.time() - start)
        if el - last_print >= 20:
            print(f"  ...battle running ({el}s / {timeout}s)")
            last_print = el
        time.sleep(1.0)
    return "timeout"


def ensure_home(win, cfg, timeout=25):
    """Makes sure we're on the village screen (clears leftover result screens)."""
    start = time.time()
    while time.time() - start < timeout:
        region, screen, vp = grab(win, cfg)
        m = match_template(screen, "attack_menu", cfg, vp=vp)
        if "error" not in m and m["found"]:
            return True
        for key in ("return_home", "surrender_dialog"):
            m = match_template(screen, key, cfg, vp=vp)
            if "error" not in m and m["found"]:
                print(f"  (clearing leftover screen: {key})")
                cx, cy = center_of(m)
                click_region_point(region, cx, cy)
                time.sleep(1.2)
                break
        time.sleep(0.5)
    print("  ! can't find the home screen. Close any open menu/popup in the game.")
    return False


def run_one_attack_cycle(win, cfg, storage_check, auto_deploy):
    """-> 'ok', 'stop' (storages full) or 'fail'."""
    print("\n" + "#" * 60)
    print("# Starting attack cycle")
    print("#" * 60)

    if not ensure_home(win, cfg):
        return "fail"

    before = None
    if storage_check:
        print("Reading resources before attack...")
        before = read_resources(win, cfg)
        print_resources("BEFORE", before)
        if is_storage_full(before, cfg):
            print("\n*** Storages are full. Stopping. ***\n")
            return "stop"

    if not wait_and_click(win, cfg, "attack_menu", "Battle button (home)"):
        return "fail"
    time.sleep(POST_CLICK_PAUSE)

    if not wait_and_click(win, cfg, "find_match", "Find a Match (Battle card)"):
        return "fail"

    # Either the army screen ("Attack!") shows up, or we go straight to scouting.
    print("Waiting for army screen / base...")
    key, m, region = wait_for_any(win, cfg, ["attack_go", "end_battle"], MATCHMAKING_TIMEOUT)
    if key is None:
        print("  ! neither the Attack! button nor the battle screen appeared")
        return "fail"
    if key == "attack_go":
        log_attempt("Attack! (army screen)", "attack_go", m, region)
        cx, cy = center_of(m)
        click_region_point(region, cx, cy)
        k2, _, _ = wait_for_any(win, cfg, ["end_battle"], MATCHMAKING_TIMEOUT)
        if k2 is None:
            print("  ! battle screen never appeared after Attack!")
            return "fail"
    print("Base loaded.")
    time.sleep(BASE_PREVIEW_WAIT)

    if auto_deploy:
        deploy_army(win, cfg)
        outcome = wait_battle_end(win, cfg, cfg["battle"]["timeout"])
    else:
        print(f"\n>>> YOUR TURN: deploy manually. Bot resumes in up to "
              f"{cfg['battle']['manual_wait']}s (or when the battle ends).\n")
        try:
            outcome = wait_battle_end(win, cfg, cfg["battle"]["manual_wait"])
        except KeyboardInterrupt:
            print("  [Ctrl+C] skipping the wait...")
            outcome = "timeout"

    if outcome == "timeout":
        print("Ending the battle (surrender)...")
        if not wait_and_click(win, cfg, "surrender_btn", "Surrender", timeout=6):
            if not wait_and_click(win, cfg, "end_battle", "End Battle", timeout=6):
                return "fail"
        time.sleep(POST_CLICK_PAUSE)
        if not wait_and_click(win, cfg, "surrender_dialog", "Surrender Okay", timeout=8):
            return "fail"
        time.sleep(POST_CLICK_PAUSE)

    if _load_template("return_home")[0] is not None:
        if not wait_and_click(win, cfg, "return_home", "Return Home", timeout=25):
            return "fail"
    else:
        time.sleep(2.5)                       # no template: tap where the button is
        rx, ry = cfg["battle"].get("return_home_pos", [0.5, 0.88])
        tap_frac(win, cfg, rx, ry)
        time.sleep(1.0)

    if storage_check:
        time.sleep(2.0)
        after = read_resources(win, cfg)
        print_resources("AFTER", after)
        if before and after:
            gained = {k: (after.get(k) - before.get(k))
                      if after.get(k) is not None and before.get(k) is not None else None
                      for k in ("gold", "elixir", "dark_elixir")}

            def fg(v):
                return f"{v:+,}" if v is not None else "n/a"
            print(f"[loot] GAINED  gold={fg(gained['gold'])}  "
                  f"elixir={fg(gained['elixir'])}  de={fg(gained['dark_elixir'])}")
            log_loot_row(before, after, gained)

    print("=== Cycle complete, back home ===\n")
    return "ok"


def mode_full_auto(cfg, storage_check, auto_deploy=True):
    if not cfg.get("viewport"):
        print("\n[!] Game area not calibrated. Run option 5 first.\n")
        return
    if storage_check:
        ensure_storage_caps(cfg)
        s = cfg["storage"]
        print(f"\nFULL AUTO + STORAGE CHECK  (stop when {s['stop_when']} of: "
              f"gold>={s['max_gold']:,}, elixir>={s['max_elixir']:,}"
              f"{', dark>=' + format(s['max_dark'], ',') if s['max_dark'] else ''})\n")
    elif auto_deploy:
        print("\nFULL AUTO - Ctrl+C to return to the menu.\n")
    else:
        print("\nAUTO ATTACK ONLY (you deploy) - Ctrl+C to return to the menu.\n")

    win = ensure_game_running()
    park_mouse()
    fails = 0
    try:
        while True:
            cfg = load_config()
            status = run_one_attack_cycle(win, cfg, storage_check, auto_deploy)
            if status == "stop":
                break
            if status == "fail":
                fails += 1
                print(f"  cycle failed ({fails}/3)")
                if fails >= 3:
                    print("\n3 failures in a row - stopping so nothing gets wasted.")
                    print("Check bot_log.csv and run option 4 to see which icon isn't matching.")
                    break
                time.sleep(5)
            else:
                fails = 0
                cd = cfg["battle"].get("cooldown", 0)
                if cd > 0:
                    print(f"Cooldown {cd}s (army training)...")
                    time.sleep(cd)
            time.sleep(1.5)
    except KeyboardInterrupt:
        print("\nCtrl+C - back to menu.")
    except pyautogui.FailSafeException:
        print("\nFail-safe triggered (mouse in a corner) - back to menu.")


# =========================================================================
# TEST / CALIBRATION MODES
# =========================================================================
def mode_test(cfg, rounds=15):
    print("\nTEST / CALIBRATE DETECTION (no clicks, Ctrl+C to stop)")
    print("Put the game on the screen you want to test (home, Multiplayer menu,")
    print("army screen, battle...). Icons that belong to other screens will 'miss' - that's normal.\n")
    win = ensure_game_running()
    totals = {k: {"scores": [], "times_ms": [], "hits": 0} for k in ASSETS}
    try:
        for r in range(1, rounds + 1):
            region, screen, vp = grab(win, cfg)
            print(f"--- round {r}/{rounds} ---")
            for asset_key in ASSETS:
                t0 = time.perf_counter()
                match = match_template(screen, asset_key, cfg, thorough=True, vp=vp)
                dt = (time.perf_counter() - t0) * 1000
                if "error" in match:
                    print(f"  {asset_key:<16} ERROR: {match['error']}")
                    continue
                totals[asset_key]["scores"].append(match["score"])
                totals[asset_key]["times_ms"].append(dt)
                if match["found"]:
                    totals[asset_key]["hits"] += 1
                status = "FOUND" if match["found"] else "miss "
                print(f"  {asset_key:<16} {status} score={match['score']:.3f} "
                      f"(need {match['threshold']:.2f}) scale={match['scale']:.2f} {dt:6.1f} ms")
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n  [Ctrl+C] stopping test early...")

    print("\n=== Summary ===")
    for k, d in totals.items():
        if not d["scores"]:
            print(f"  {k:<16} no reads")
            continue
        n = len(d["scores"])
        print(f"  {k:<16} avg_score={sum(d['scores'])/n:.3f}  "
              f"avg_time={sum(d['times_ms'])/n:6.1f} ms  hit_rate={100.0*d['hits']/n:5.1f}%")
    print()


def mode_calibrate(cfg):
    """Teaches the bot where the game picture is inside the emulator window."""
    win = ensure_game_running()
    region = get_region(win, force_activate=True)
    print("\nCALIBRATE GAME AREA")
    print("The 'game picture' is the actual Clash of Clans image - NOT the emulator's")
    print("title bar or side toolbar. Open the HOME village screen, keep the window")
    print("where you will use it, and do not touch the window during the countdowns.\n")

    pts = []
    for label in ("TOP-LEFT corner", "BOTTOM-RIGHT corner"):
        print(f"Move the mouse onto the {label} of the game picture.")
        for s in range(8, 0, -1):
            print(f"   reading position in {s}s ", end="\r")
            time.sleep(1)
        p = pyautogui.position()
        pts.append((p.x, p.y))
        print(f"   {label}: {p.x}, {p.y}          ")

    left, top, ww, wh = get_region(win)
    (x0, y0), (x1, y1) = pts
    w, h = x1 - x0, y1 - y0
    if w < 300 or h < 200:
        print("\n[!] That rectangle is too small. Try again (corners in the right order).")
        park_mouse()
        return
    ratio = w / h
    if abs(ratio - 16 / 9) > 0.25:
        print(f"\n[!] Warning: shape is {ratio:.2f}:1 (the game is normally ~1.78:1). "
              "Check the corners.")

    cfg["viewport"] = {"dx": x0 - left, "dy": y0 - top, "w": w, "h": h,
                       "win_w": ww, "win_h": wh}
    save_config(cfg)
    park_mouse()

    os.makedirs(DEBUG_DIR, exist_ok=True)
    region = get_region(win)
    shot = capture(region)
    vp = vp_in_window(cfg)
    cv2.rectangle(shot, (vp[0], vp[1]), (vp[0] + vp[2], vp[1] + vp[3]), (0, 255, 0), 3)
    path = os.path.join(DEBUG_DIR, "viewport.png")
    cv2.imwrite(path, shot)
    print(f"\nSaved. Game area = {w}x{h} px, offset ({cfg['viewport']['dx']}, {cfg['viewport']['dy']}) in the window.")
    print(f"Open {path} - the green rectangle must hug the game picture exactly.")
    print("If it doesn't, run option 5 again.\n")
    try:
        os.startfile(path)
    except Exception:
        pass


def mode_preview(cfg):
    """Draws the deploy points / card positions on a live screenshot and
    optionally tests the keyboard hotkeys. Nothing is deployed."""
    win = ensure_game_running()
    park_mouse()
    region = get_region(win, force_activate=True)
    time.sleep(0.5)
    screen = capture(region)
    vp = vp_in_window(cfg)
    if vp is None:
        print("[!] Game area not calibrated - using the whole window (run option 5).")
        vp = (0, 0, region[2], region[3])

    def P(fx, fy):
        return int(vp[0] + fx * vp[2]), int(vp[1] + fy * vp[3])

    d = cfg["deploy"]
    out = screen.copy()
    cv2.rectangle(out, (vp[0], vp[1]), (vp[0] + vp[2], vp[1] + vp[3]), (0, 255, 0), 2)
    for side, col in (("NW", (0, 255, 255)), ("NE", (0, 200, 255)),
                      ("SE", (0, 255, 128)), ("SW", (255, 255, 0))):
        for _ in range(25):
            p = ring_point(d, side)
            if p:
                cv2.circle(out, P(*p), 5, col, 2)
    b = d["base"]
    pts = [(b["cx"] - b["hw"], b["cy"]), (b["cx"], b["cy"] - b["hh"]),
           (b["cx"] + b["hw"], b["cy"]), (b["cx"], b["cy"] + b["hh"])]
    cv2.polylines(out, [np.array([P(*q) for q in pts], np.int32)], True, (0, 0, 255), 2)
    for _ in range(15):
        cv2.circle(out, P(*base_point(d)), 4, (255, 0, 255), -1)
    for k, fx in d["cards"].items():
        c = P(fx, d["card_y"])
        cv2.circle(out, c, 8, (255, 128, 0), 2)
        cv2.putText(out, k.upper(), (c[0] - 6, c[1] - 14), 0, 0.55, (255, 128, 0), 2)
    try:
        vv = d["verify"]
        st = read_cards(win, cfg, list(d["cards"].keys()))
        print("\nCard bar (coloured = has units, grey = empty):")
        for k, (c, fr) in st.items():
            print(f"   {k.upper()}: {'HAS UNITS' if c else 'empty/grey'}  ({fr:.0%} coloured)")
            a2 = P(d["cards"][k] - vv["patch_w"] / 2, d["card_y"] - vv["patch_h"] / 2)
            b2 = P(d["cards"][k] + vv["patch_w"] / 2, d["card_y"] + vv["patch_h"] / 2)
            cv2.rectangle(out, a2, b2, (0, 255, 0) if c else (0, 0, 255), 1)
    except Exception as e:
        print(f"  (card read failed: {e})")
    for name, (fx, fy, fw, fh) in cfg["regions"].items():
        a, b = P(fx, fy), P(fx + fw, fy + fh)
        cv2.rectangle(out, a, b, (255, 0, 255), 1)

    os.makedirs(DEBUG_DIR, exist_ok=True)
    path = os.path.join(DEBUG_DIR, "deploy_preview.png")
    cv2.imwrite(path, out)
    print(f"\nSaved {path}")
    print("  yellow circles = where dragons/heroes will be tapped (must be OUTSIDE the red")
    print("                   zone, on grass, away from every button)")
    print("  orange circles = troop cards (only used when use_hotkeys = false)")
    print("  magenta boxes  = where the storage numbers are read (home screen)")
    try:
        os.startfile(path)
    except Exception:
        pass

    ans = input("\nTest the hotkeys now? Switch your eyes to the game: the bot will select\n"
                "the dragon card, then hero Q, then hero W (nothing gets deployed). [y/N] ").strip().lower()
    if ans == "y":
        get_region(win, force_activate=True)
        time.sleep(1.0)
        for name, key in [(u["name"], u["key"]) for u in d["units"][:3]]:
            print(f"  pressing '{key}' ({name}) - the card should highlight/lift")
            press_key(key)
            time.sleep(2.0)
        print("If the cards did NOT react, set \"use_hotkeys\": false in config.json (deploy section).\n")


# =========================================================================
# MAIN
# =========================================================================
def parse_args():
    p = argparse.ArgumentParser(description="Clash of Clans Auto-Attack Bot")
    p.add_argument("--gpu", action="store_true",
                   help="use cv2.cuda template matching if available")
    p.add_argument("--fast", action="store_true",
                   help="(kept for compatibility - fast matching is now always on)")
    p.add_argument("--rounds", type=int, default=15,
                   help="scan rounds for mode 4")
    return p.parse_args()


def main():
    global USE_GPU
    args = parse_args()
    USE_GPU = args.gpu

    print("Loading config...")
    cfg = load_config()
    ensure_log_header()

    while True:
        print_menu(cfg)
        try:
            choice = input("Choose [0-6]: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nExiting.")
            break

        cfg = load_config()
        try:
            if choice == "0":
                print("Exiting.")
                break
            elif choice == "1":
                if _ensure_full_deps(need_ocr=False):
                    mode_full_auto(cfg, storage_check=False, auto_deploy=True)
            elif choice == "2":
                if _ensure_full_deps(need_ocr=True):
                    mode_full_auto(cfg, storage_check=True, auto_deploy=True)
            elif choice == "3":
                if _ensure_full_deps(need_ocr=False):
                    mode_full_auto(cfg, storage_check=False, auto_deploy=False)
            elif choice == "4":
                if _ensure_full_deps(need_ocr=False):
                    mode_test(cfg, rounds=args.rounds)
            elif choice == "5":
                if _ensure_full_deps(need_ocr=False):
                    mode_calibrate(cfg)
            elif choice == "6":
                if _ensure_full_deps(need_ocr=False):
                    mode_preview(cfg)
            else:
                print(f"Unknown choice: {choice!r}")
        except RuntimeError as e:
            print(f"\n[!] {e}")
        except KeyboardInterrupt:
            print("\nCtrl+C - back to menu.")
        except Exception as e:  # pyautogui.FailSafeException etc.
            print(f"\n[!] {type(e).__name__}: {e}")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    finally:
        try:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        except Exception:
            pass
