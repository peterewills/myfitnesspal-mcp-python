"""
MyFitnessPal MCP Server

A Model Context Protocol (MCP) server that provides tools for interacting
with MyFitnessPal data including food diary, exercises, measurements, goals,
water intake, and food search.

Authentication Methods (in order of priority):
1. Environment variables: MFP_USERNAME and MFP_PASSWORD
2. Stored session cookies: ~/.mfp_mcp/cookies.json
3. Chromium-based browser cookies (macOS): Arc, Chrome, Edge, Brave, Vivaldi,
   Opera, and any other installed Chromium browser detected via the keychain
   "Safe Storage" entry.
4. browser_cookie3 fallback (legacy Chrome/Firefox paths on any OS)
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from datetime import date, datetime, timedelta
from http.cookiejar import CookieJar, Cookie
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
import time
from cryptography.fernet import Fernet
import keyring

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import Icon
from pydantic import BaseModel, Field, ConfigDict, field_validator

# Configure logging to stderr (required for stdio transport)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stderr)],
)
logger = logging.getLogger("mfp_mcp")

# MyFitnessPal's app icon, shown by clients that render server icons
ICON_PNG = (Path(__file__).parent / "icon.png").read_bytes()

# Initialize MCP server
mcp = FastMCP(
    "myfitnesspal_mcp",
    icons=[Icon(
        src="data:image/png;base64," + base64.b64encode(ICON_PNG).decode(),
        mimeType="image/png",
        sizes=["180x180"],
    )],
    instructions=(
        "Before logging a meal, read the day's diary with mfp_get_diary: the user may "
        "have already logged it, and adding again double-counts.\n"
        "Custom foods cannot be edited. To correct one, create the fixed version "
        "with mfp_create_custom_food, then delete the old one with "
        "mfp_delete_custom_food.\n"
        "Custom-food carbs are NET carbs under the default country_code.\n"
        "Prefer the user's own recipes and custom foods over database foods: "
        "mfp_search_food lists them first. Log any search result, recipe or not, with "
        "mfp_add_food_to_diary and its mfp_id."
    ),
)

# Configuration paths. MFP_STATE_DIR points remote deployments at a persistent
# volume, so refreshed session cookies survive restarts.
CONFIG_DIR = Path(os.environ.get("MFP_STATE_DIR") or Path.home() / ".mfp_mcp")
COOKIES_FILE = CONFIG_DIR / "cookies.json"

# MyFitnessPal's v2 JSON API, used for diary writes. The legacy HTML form
# endpoint (/food/diary/{user}/add) was removed by MFP and now returns 404.
MFP_API_BASE = "https://api.myfitnesspal.com"
MFP_CLIENT_ID = "mfp-main-js"
VALID_MEALS = ("Breakfast", "Lunch", "Dinner", "Snacks")


# ============================================================================
# Authentication Helper Functions
# ============================================================================


def ensure_config_dir():
    """Ensure the config directory exists."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_DIR.chmod(0o700)


def save_cookies(cookies: Dict[str, str]):
    """
    Save session cookies to file for persistence.
    
    Args:
        cookies: Dictionary of cookie name -> value
    """
    ensure_config_dir()
    cookie_data = {
        "cookies": cookies,
        "saved_at": datetime.now().isoformat(),
        "seed": _env_cookies_seed(),
    }
    with open(COOKIES_FILE, "w") as f:
        json.dump(cookie_data, f, indent=2)
    # Session cookies grant full account access - restrict to owner only
    COOKIES_FILE.chmod(0o600)
    logger.info(f"Saved session cookies to {COOKIES_FILE}")


def _env_cookies_seed() -> Optional[str]:
    """Fingerprint of MFP_COOKIES_JSON, recorded with saved cookies so a new
    export can be told apart from the one the saved cookies descend from."""
    env_cookies = os.environ.get("MFP_COOKIES_JSON")
    return hashlib.sha256(env_cookies.encode()).hexdigest() if env_cookies else None


def _parse_env_cookies() -> Optional[Dict[str, str]]:
    env_cookies = os.environ.get("MFP_COOKIES_JSON")
    if not env_cookies:
        return None
    try:
        cookies = json.loads(env_cookies)
        if isinstance(cookies, dict) and cookies:
            return cookies
        logger.warning("MFP_COOKIES_JSON is not a non-empty JSON object; ignoring it")
    except json.JSONDecodeError as e:
        logger.warning(f"MFP_COOKIES_JSON is not valid JSON; ignoring it: {e}")
    return None


def load_cookies() -> Optional[Dict[str, str]]:
    """
    Load session cookies from file, or from the MFP_COOKIES_JSON environment variable.

    MFP_COOKIES_JSON (a JSON object of cookie name -> value) is how remote
    deployments, which have no browser to read from, receive a session. The
    server then saves refreshed cookies to file (see refresh_session). The file
    wins only while it descends from the current MFP_COOKIES_JSON; a new export
    replaces it.

    Returns:
        Dictionary of cookies if found and valid, None otherwise
    """
    env_cookies = _parse_env_cookies()

    if not COOKIES_FILE.exists():
        return env_cookies

    try:
        with open(COOKIES_FILE, "r") as f:
            cookie_data = json.load(f)

        if env_cookies and cookie_data.get("seed") != _env_cookies_seed():
            return env_cookies
        
        # Check if cookies are less than 30 days old
        saved_at = datetime.fromisoformat(cookie_data.get("saved_at", "2000-01-01"))
        if datetime.now() - saved_at > timedelta(days=30):
            logger.info("Stored cookies are expired (>30 days old)")
            return env_cookies

        return cookie_data.get("cookies")
    except Exception as e:
        logger.warning(f"Failed to load cookies: {e}")
        return env_cookies


def dict_to_cookiejar(cookies_dict: Dict[str, str], domain: str = ".myfitnesspal.com") -> CookieJar:
    """
    Convert a dictionary of cookies to a CookieJar that can be used by myfitnesspal.Client.
    
    Args:
        cookies_dict: Dictionary of cookie name -> value
        domain: Domain for the cookies (default: .myfitnesspal.com)
    
    Returns:
        CookieJar: A CookieJar object populated with the cookies
    """
    jar = CookieJar()
    
    for name, value in cookies_dict.items():
        cookie = Cookie(
            version=0,
            name=name,
            value=value,
            port=None,
            port_specified=False,
            domain=domain,
            domain_specified=True,
            domain_initial_dot=domain.startswith('.'),
            path="/",
            path_specified=True,
            secure=True,
            expires=int(time.time()) + 86400 * 30,  # 30 days from now
            discard=False,
            comment=None,
            comment_url=None,
            rest={"HttpOnly": None},
            rfc2109=False,
        )
        jar.set_cookie(cookie)
    
    return jar

# ============================================================================
# Chromium Browser Cookie Extraction (macOS)
# ============================================================================
#
# Chromium-based browsers (Arc, Chrome, Edge, Brave, Vivaldi, Opera, ...)
# store cookies in a SQLite database with each value encrypted using
# AES-128-CBC. The encryption key is derived from a per-browser password
# stored in the macOS Keychain under a service name like
# "<Browser> Safe Storage".
#
# We discover installed Chromium browsers by listing keychain "Safe Storage"
# entries and try each one until we find a valid MyFitnessPal session token.
# This is what lets the MCP "just work" when the user logs in via any modern
# browser — including Arc, which `browser_cookie3` does not support.

# Cookies DB locations relative to ~/Library/Application Support/.
# Newer Chromium versions moved the cookies DB into a "Network/" subdir;
# we try the new path first, falling back to the legacy location.
_CHROMIUM_COOKIES_PATHS_MACOS: Dict[str, List[str]] = {
    "Arc":            ["Arc/User Data/Default/Network/Cookies",
                       "Arc/User Data/Default/Cookies"],
    "Chrome":         ["Google/Chrome/Default/Network/Cookies",
                       "Google/Chrome/Default/Cookies"],
    "Chromium":       ["Chromium/Default/Network/Cookies",
                       "Chromium/Default/Cookies"],
    "Microsoft Edge": ["Microsoft Edge/Default/Network/Cookies",
                       "Microsoft Edge/Default/Cookies"],
    "Brave":          ["BraveSoftware/Brave-Browser/Default/Network/Cookies",
                       "BraveSoftware/Brave-Browser/Default/Cookies"],
    "Vivaldi":        ["Vivaldi/Default/Network/Cookies",
                       "Vivaldi/Default/Cookies"],
    "Opera":          ["com.operasoftware.Opera/Network/Cookies",
                       "com.operasoftware.Opera/Cookies"],
}

# Friendly browser names accepted by `refresh_browser_cookies("<name>")`
# mapped to the canonical "Safe Storage" service prefix.
_CHROMIUM_BROWSER_ALIASES: Dict[str, str] = {
    "arc": "Arc",
    "chrome": "Chrome",
    "chromium": "Chromium",
    "edge": "Microsoft Edge",
    "brave": "Brave",
    "vivaldi": "Vivaldi",
    "opera": "Opera",
}


def _safe_storage_keychain_password(service_name: str) -> Optional[bytes]:
    """Look up `service_name` in the macOS Keychain and return the raw bytes.

    Returns None if the entry doesn't exist or access is denied.

    NOTE: on a fresh install the first read of another app's Safe Storage
    entry triggers a macOS keychain authorization dialog ("<app> wants to
    use information stored in your keychain"). If this MCP is running
    headless (e.g. spawned by Claude Desktop with no UI focus), the prompt
    is silently denied and this call returns None after the 5s timeout.
    The user only needs to click "Always Allow" once, but they need to
    know to look for the prompt — the README troubleshooting section
    documents this.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", service_name, "-w"],
            capture_output=True, check=True, timeout=5,
        )
        return result.stdout.strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        return None


def _list_chromium_safe_storage_services_macos() -> List[str]:
    """Return all keychain service names ending in 'Safe Storage'.

    These identify installed Chromium-based browsers. We don't hard-code
    the list — anything matching the pattern is fair game.
    """
    keychain_path = os.path.expanduser("~/Library/Keychains/login.keychain-db")
    try:
        result = subprocess.run(
            ["security", "dump-keychain", keychain_path],
            capture_output=True, check=True, timeout=10,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired,
            FileNotFoundError):
        return []
    services = set()
    text = result.stdout.decode("utf-8", errors="replace")
    for line in text.splitlines():
        # The `svce` attribute appears as: "svce"<blob>="Arc Safe Storage"
        if '"svce"<blob>=' not in line or "Safe Storage" not in line:
            continue
        try:
            value = line.split('"svce"<blob>=', 1)[1].strip()
            value = value.strip('"')
            if value.endswith("Safe Storage"):
                services.add(value)
        except IndexError:
            continue
    return sorted(services)


def _derive_chromium_aes_key_macos(safe_storage_password: bytes) -> bytes:
    """Derive the AES-128 cookie key Chromium uses on macOS.

    Per Chromium's `os_crypt_mac.mm`: PBKDF2-HMAC-SHA1 with salt='saltysalt',
    1003 iterations, 16-byte key.
    """
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.backends import default_backend
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA1(),
        length=16,
        salt=b"saltysalt",
        iterations=1003,
        backend=default_backend(),
    )
    return kdf.derive(safe_storage_password)


def _decrypt_chromium_value_macos(encrypted_value: bytes,
                                   aes_key: bytes,
                                   host_key: str = "") -> Optional[str]:
    """Decrypt a single Chromium cookie `encrypted_value`. Returns None on
    failure or for unsupported schemes (e.g. v20 app-bound encryption).

    `host_key` is the cookie's host column from SQLite; modern Chromium
    prepends `SHA-256(host_key)` to the plaintext as an integrity tag, so
    we strip exactly that 32-byte prefix when it's present. Without this
    check, long ASCII cookie values from legacy rows would be silently
    truncated by 32 bytes (the shortened plaintext still decodes as UTF-8).
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend
    if not encrypted_value or len(encrypted_value) < 3:
        return None
    prefix = encrypted_value[:3]
    if prefix not in (b"v10", b"v11"):
        # v20 needs app-bound decryption via the browser process and is not
        # supported here. Caller should fall back to a different source.
        return None
    try:
        cipher = Cipher(
            algorithms.AES(aes_key),
            modes.CBC(b" " * 16),
            backend=default_backend(),
        )
        decryptor = cipher.decryptor()
        plaintext = decryptor.update(encrypted_value[3:]) + decryptor.finalize()
    except Exception:
        return None
    # Strip PKCS#7 padding.
    if not plaintext:
        return None
    pad_len = plaintext[-1]
    if pad_len < 1 or pad_len > 16:
        return None
    plaintext = plaintext[:-pad_len]
    # Strip the SHA-256(host_key) integrity prefix only when it actually
    # matches — never blindly. Legacy rows without the prefix have shorter
    # but otherwise normal plaintexts.
    if host_key and len(plaintext) >= 32:
        expected_prefix = hashlib.sha256(host_key.encode("utf-8")).digest()
        if plaintext[:32] == expected_prefix:
            plaintext = plaintext[32:]
    try:
        return plaintext.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None


def _snapshot_sqlite_db(src: Path, dst: str) -> None:
    """Copy a live SQLite DB into `dst` using the backup API.

    The browser's cookies DB may be open in WAL mode with active writers;
    a plain `shutil.copy` misses committed rows that still live in the
    `-wal` sidecar. The backup API handles WAL/SHM correctly, takes a
    consistent snapshot, and doesn't require taking a write lock — opening
    the source read-only is enough.
    """
    src_con = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        dst_con = sqlite3.connect(dst)
        try:
            src_con.backup(dst_con)
        finally:
            dst_con.close()
    finally:
        src_con.close()


def _extract_chromium_cookies_macos(
    cookies_db_path: Path,
    aes_key: bytes,
    domain: str = "myfitnesspal.com",
) -> Dict[str, str]:
    """Read cookies for `domain` (and its subdomains) from a Chromium DB.

    The DB is snapshotted via the SQLite backup API so rows pending in the
    `-wal` file are included. Cookies whose decrypted value isn't clean
    UTF-8 are skipped — those can't go into HTTP headers anyway.
    """
    # `mkstemp` gives us a uniquely-named file we own, immune to the
    # time-of-check/time-of-use race that `mktemp` would create.
    fd, tmp_path = tempfile.mkstemp(suffix=".cookies.db")
    os.close(fd)
    try:
        _snapshot_sqlite_db(cookies_db_path, tmp_path)
        con = sqlite3.connect(tmp_path)
        try:
            # `host_key = 'myfitnesspal.com' OR host_key LIKE '%.myfitnesspal.com'`
            # — exact match + any subdomain. Avoids matching unrelated hosts
            # like `notmyfitnesspal.com` that the loose LIKE pattern would.
            rows = con.execute(
                "SELECT name, value, encrypted_value, host_key FROM cookies "
                "WHERE host_key = ? OR host_key LIKE ?",
                (domain, f"%.{domain}"),
            ).fetchall()
        finally:
            con.close()
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
    cookies: Dict[str, str] = {}
    for name, plain, enc, host_key in rows:
        value = (
            plain if plain
            else _decrypt_chromium_value_macos(enc, aes_key, host_key)
        )
        if value is None or "�" in value:
            continue
        cookies[name] = value
    return cookies


def _has_real_mfp_session(cookies: Dict[str, str]) -> bool:
    """True if the cookie set looks like an authenticated MFP session.

    A pre-auth response can include cookies with 'auth' in the name
    (e.g. `__Host-next-auth.csrf-token`), so we look for the specific
    session-token markers MFP actually uses.
    """
    return any(
        "session-token" in name or name == "_mfp_session"
        for name in cookies
    )


def _try_extract_from_chromium_browser(
    service: str,
) -> Optional[Dict[str, str]]:
    """Extract cookies from one specific Chromium browser by Safe Storage
    service name (e.g. 'Arc Safe Storage'). Returns None on any failure."""
    browser_name = service.replace(" Safe Storage", "").strip()
    relative_paths = _CHROMIUM_COOKIES_PATHS_MACOS.get(browser_name)
    if not relative_paths:
        logger.debug(f"No cookies DB path mapping for '{browser_name}'")
        return None
    appsup = Path.home() / "Library" / "Application Support"
    db_path = next(
        (appsup / p for p in relative_paths if (appsup / p).exists()),
        None,
    )
    if not db_path:
        logger.debug(f"No cookies DB found for '{browser_name}'")
        return None
    password = _safe_storage_keychain_password(service)
    if not password:
        logger.debug(f"Keychain lookup failed for '{service}'")
        return None
    try:
        aes_key = _derive_chromium_aes_key_macos(password)
        return _extract_chromium_cookies_macos(db_path, aes_key)
    except Exception as e:
        logger.debug(f"Cookie extraction failed for '{browser_name}': {e}")
        return None


def try_chromium_browsers_for_session_cookies(
) -> Optional[Tuple[str, Dict[str, str]]]:
    """Discover installed Chromium browsers (macOS only) and return the first
    one that has a valid MyFitnessPal session token.

    Returns a (browser_name, cookies) tuple, or None if no browser yielded
    a usable session.
    """
    if sys.platform != "darwin":
        return None
    services = _list_chromium_safe_storage_services_macos()
    if not services:
        logger.debug("No Chromium Safe Storage entries found in keychain")
        return None
    for service in services:
        cookies = _try_extract_from_chromium_browser(service)
        if not cookies:
            continue
        browser_name = service.replace(" Safe Storage", "").strip()
        if _has_real_mfp_session(cookies):
            logger.info(
                f"Found valid MyFitnessPal session in {browser_name} "
                f"({len(cookies)} cookies)"
            )
            return browser_name, cookies
        logger.debug(
            f"{browser_name} had {len(cookies)} cookies but no session token"
        )
    return None


def looks_like_fernet_token(value: str) -> bool:
    """Return True if the value appears to be a Fernet token."""
    if not value:
        return False
    # Fernet tokens are URL-safe base64-encoded and typically begin with "gAAAAA".
    # Use a lightweight prefix check so plaintext credentials continue to work
    # even when MFP_SECRET_KEY is configured.
    #
    # Edge case: a genuine plaintext password that starts with "gAAAAA" is
    # misclassified as a ciphertext, fails Fernet decryption, and
    # `get_decrypted_credential` returns None — credential auth is silently
    # skipped and the caller falls through to cookie-based auth. Extremely
    # unlikely for a real password, and the fallback is graceful, so we
    # accept this heuristic rather than adding a length/base64 check.
    return value.startswith("gAAAAA")


KEYRING_SERVICE = "mfp-mcp"
KEYRING_SECRET_KEY_ACCOUNT = "MFP_SECRET_KEY"


def get_secret_key() -> Optional[str]:
    """Resolves MFP_SECRET_KEY from, in order:
    1. The MFP_SECRET_KEY environment variable.
    2. The OS keychain (service: 'mfp-mcp', account: 'MFP_SECRET_KEY').

    Returns the key string, or None if not found in either location.
    """
    key = os.environ.get("MFP_SECRET_KEY")
    if key:
        logger.info("MFP_SECRET_KEY loaded from environment variable.")
        return key

    try:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_SECRET_KEY_ACCOUNT)
        if key:
            logger.info("MFP_SECRET_KEY loaded from OS keychain.")
            return key
    except Exception as e:
        logger.warning(f"Keychain lookup failed: {e}")

    return None


def get_decrypted_credential(env_var_name: str) -> Optional[str]:
    """Retrieves credentials from environment variables, decrypting Fernet tokens when needed.

    The decryption key (MFP_SECRET_KEY) is resolved from the environment variable first,
    then from the OS keychain as a fallback.

    Returns the decrypted string on success, the raw value when no decryption is needed,
    or None if the env var is missing, decryption fails, or the value looks encrypted but
    no key is available (to avoid passing ciphertext to the auth flow).
    """
    encrypted_value = os.environ.get(env_var_name)

    if not encrypted_value:
        logger.warning(f"Missing {env_var_name}.")
        return None

    if not looks_like_fernet_token(encrypted_value):
        # Plain-text credential — no key lookup needed.
        return encrypted_value

    # Value looks like a Fernet token; resolve the secret key only now.
    secret_key = get_secret_key()

    if not secret_key:
        logger.warning(
            f"{env_var_name} appears to be encrypted but MFP_SECRET_KEY is not set. "
            "Credential auth will be skipped to avoid passing ciphertext to the auth flow."
        )
        return None

    try:
        f = Fernet(secret_key.encode())
        return f.decrypt(encrypted_value.encode()).decode()
    except Exception as e:
        logger.error(f"Decryption failed for {env_var_name}: {e}")
        return None

def authenticate_with_credentials(username: str, password: str) -> Dict[str, str]:
    """
    Authenticate with MyFitnessPal using username/password.
    
    Args:
        username: MyFitnessPal username or email
        password: MyFitnessPal password
    
    Returns:
        Dictionary of session cookies
        
    Raises:
        RuntimeError: If authentication fails
    """
    # Log authentication attempt without exposing the username
    logger.info("Authenticating with credentials")
    
    # MyFitnessPal login URL and endpoints
    LOGIN_URL = "https://www.myfitnesspal.com/account/login"
    
    try:
        with httpx.Client(follow_redirects=True, timeout=30.0) as client:
            # First, get the login page to obtain CSRF token
            response = client.get(LOGIN_URL)
            response.raise_for_status()
            
            # Extract CSRF token from cookies or page
            cookies = dict(response.cookies)
            
            # Attempt login
            login_data = {
                "username": username,
                "password": password,
            }
            
            # Try the standard form login
            login_response = client.post(
                LOGIN_URL,
                data=login_data,
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Referer": LOGIN_URL,
                },
            )
            
            # MyFitnessPal moved to a NextAuth backend, so the legacy form
            # POST flow this function uses no longer actually logs the user
            # in — the endpoint just returns HTTP 200 with a fresh CSRF
            # cookie. The old success check matched any cookie containing
            # 'auth' (which `__Host-next-auth.csrf-token` does), reporting
            # success and overwriting cookies.json with useless pre-auth
            # cookies. Require a real session token before claiming success.
            all_cookies = dict(client.cookies)
            if _has_real_mfp_session(all_cookies):
                logger.info("Successfully authenticated with credentials")
                return all_cookies
            raise RuntimeError(
                "Login appeared to fail — response contained no session token. "
                "MyFitnessPal's form login flow does not work against the "
                "current NextAuth backend. Log into myfitnesspal.com in any "
                "Chromium-based browser (Arc, Chrome, Edge, Brave, ...) and "
                "the MCP will pick up the session automatically."
            )
                
    except httpx.HTTPError as e:
        raise RuntimeError(f"HTTP error during authentication: {e}")
    except Exception as e:
        raise RuntimeError(f"Authentication failed: {e}")


def get_mfp_client():
    """
    Get an authenticated MyFitnessPal client.

    Authentication is attempted in this order:
    1. Environment variables (MFP_USERNAME, MFP_PASSWORD)
       a. First tries previously-cached cookies for this user.
       b. Then falls back to form login (only useful on legacy accounts).
    2. Stored session cookies (~/.mfp_mcp/cookies.json)
    3. Chromium-based browser cookies (macOS): auto-discovers Arc, Chrome,
       Edge, Brave, Vivaldi, Opera, or any other installed Chromium browser
       via the keychain's "Safe Storage" entries.
    4. `browser_cookie3` default fallback (legacy Chrome/Firefox paths).

    Returns:
        myfitnesspal.Client: Authenticated client instance

    Raises:
        RuntimeError: If all authentication methods fail
    """
    import myfitnesspal
    import requests as _requests
    import cloudscraper as _cloudscraper
    # cloudscraper alters TLS fingerprints in a way that triggers MFP's
    # Cloudflare 403. Plain requests works fine with the browser session cookies.
    _cloudscraper.create_scraper = lambda **kw: _requests.Session()

    last_error = None

    # Method 1: Try environment variable credentials
    username = get_decrypted_credential("MFP_USERNAME")
    password = get_decrypted_credential("MFP_PASSWORD")

    if username and password:
        logger.info("Attempting authentication with environment credentials")

        # First check if we have valid stored cookies from a previous credential auth
        stored_cookies = load_cookies()
        if stored_cookies:
            logger.info("Found stored session cookies, testing validity...")
            try:
                cookiejar = dict_to_cookiejar(stored_cookies)
                client = myfitnesspal.Client(cookiejar=cookiejar)
                logger.info("Stored cookies are valid")
                return client
            except Exception as e:
                logger.info(f"Stored cookies invalid: {e}, re-authenticating...")

        # Authenticate with credentials and save cookies
        try:
            cookies = authenticate_with_credentials(username, password)
            save_cookies(cookies)

            # Create client with the new cookies
            cookiejar = dict_to_cookiejar(cookies)
            client = myfitnesspal.Client(cookiejar=cookiejar)
            logger.info("Successfully authenticated with credentials")
            return client

        except Exception as e:
            last_error = e
            logger.warning(f"Credential authentication failed: {e}")
            # Fall through to other methods

    # Method 2: Try stored session cookies (without credential auth)
    stored_cookies = load_cookies()
    if stored_cookies:
        logger.info("Attempting authentication with stored cookies")
        try:
            cookiejar = dict_to_cookiejar(stored_cookies)
            client = myfitnesspal.Client(cookiejar=cookiejar)
            logger.info("Successfully authenticated with stored cookies")
            return client
        except Exception as e:
            last_error = e
            logger.warning(f"Stored cookie authentication failed: {e}")

    # Method 3: Auto-discover Chromium-based browsers (macOS) and pull a live
    # session from whichever one is logged into MFP. This works for Arc,
    # Chrome, Edge, Brave, Vivaldi, Opera, etc. — anything that registers a
    # "<Browser> Safe Storage" entry in the macOS keychain.
    logger.info("Attempting authentication via Chromium browser auto-discovery")
    try:
        result = try_chromium_browsers_for_session_cookies()
        if result:
            browser_name, chromium_cookies = result
            cookiejar = dict_to_cookiejar(chromium_cookies)
            client = myfitnesspal.Client(cookiejar=cookiejar)
            # Only persist after we've verified it works, so a transient
            # failure can't poison cookies.json.
            save_cookies(chromium_cookies)
            logger.info(
                f"Successfully authenticated via Chromium auto-discovery "
                f"({browser_name})"
            )
            return client
        logger.info("No Chromium browser had a usable MFP session")
    except Exception as e:
        last_error = e
        logger.warning(f"Chromium auto-discovery authentication failed: {e}")

    # Method 4: Try browser cookies via browser_cookie3 (legacy fallback)
    logger.info("Attempting authentication with browser_cookie3 fallback")
    try:
        client = myfitnesspal.Client()
        logger.info("Successfully authenticated with browser cookies")
        return client
    except Exception as e:
        last_error = e
        raise RuntimeError(
            f"All authentication methods failed. Last error: {str(last_error)}\n\n"
            "Please try one of these solutions:\n"
            "1. Log into myfitnesspal.com in any Chromium-based browser "
            "(Arc, Chrome, Edge, Brave, Vivaldi, Opera, ...) — the MCP will "
            "auto-discover the session on macOS.\n"
            "2. Set MFP_USERNAME and MFP_PASSWORD in Claude Desktop config "
            "(legacy form-login flow; rarely works against the current "
            "NextAuth backend).\n"
            "3. Manually populate ~/.mfp_mcp/cookies.json with a valid "
            "session token."
        )


# ============================================================================
# Data Formatting Helper Functions
# ============================================================================


def parse_date(date_str: Optional[str] = None) -> date:
    """
    Parse a date string or return today's date.

    Args:
        date_str: Date in YYYY-MM-DD format, or None for today

    Returns:
        date: Parsed date object
    """
    if date_str is None:
        return date.today()
    return datetime.strptime(date_str, "%Y-%m-%d").date()


class ResponseFormat(str, Enum):
    """Output format for tool responses."""

    MARKDOWN = "markdown"
    JSON = "json"


def format_response(data: Any, format_type: ResponseFormat, title: str = "") -> str:
    """
    Format response data based on requested format.

    Args:
        data: Data to format
        format_type: Output format (markdown or json)
        title: Optional title for markdown format

    Returns:
        str: Formatted response string
    """
    if format_type == ResponseFormat.JSON:
        return json.dumps(data, indent=2, default=str)

    # Markdown format
    lines = []
    if title:
        lines.append(f"## {title}\n")

    if isinstance(data, dict):
        # Scalars FIRST, then nested sections. A top-level scalar emitted after
        # a "### section" is visually absorbed into that section: the diary's
        # top-level `water` rendered directly below `### daily_goals`, which
        # reads as the water GOAL rather than the amount actually logged.
        # (Real misread, 2026-07-30.) Ordering keeps every scalar unambiguous.
        for key, value in data.items():
            if not isinstance(value, (dict, list)):
                lines.append(f"- **{key}**: {value}")
        for key, value in data.items():
            if isinstance(value, dict):
                lines.append(f"### {key}")
                for k, v in value.items():
                    lines.append(f"- **{k}**: {v}")
            elif isinstance(value, list):
                lines.append(f"### {key}")
                for item in value:
                    if isinstance(item, dict):
                        lines.append(f"- {item.get('name', str(item))}")
                        for k, v in item.items():
                            if k != "name":
                                lines.append(f"  - {k}: {v}")
                    else:
                        lines.append(f"- {item}")
    else:
        lines.append(str(data))

    return "\n".join(lines)


# ============================================================================
# Pydantic Input Models
# ============================================================================


class GetDiaryInput(BaseModel):
    """Input model for getting food diary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class SearchFoodInput(BaseModel):
    """Input model for searching foods."""

    model_config = ConfigDict(str_strip_whitespace=True)

    query: str = Field(
        ...,
        description="Search query for food items (e.g., 'chicken breast', 'apple')",
        min_length=1,
        max_length=200,
    )
    limit: int = Field(
        default=10,
        description="Maximum number of results to return",
        ge=1,
        le=50,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class GetFoodDetailsInput(BaseModel):
    """Input model for getting food item details."""

    model_config = ConfigDict(str_strip_whitespace=True)

    mfp_id: str = Field(
        ...,
        description="MyFitnessPal food item ID (obtained from search results)",
        min_length=1,
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class AddFoodToDiaryInput(BaseModel):
    """Input model for adding food to diary."""

    model_config = ConfigDict(str_strip_whitespace=True)

    mfp_id: str = Field(
        ...,
        description="MyFitnessPal food item ID (obtained from mfp_search_food)",
        min_length=1,
    )
    meal: str = Field(
        default="Breakfast",
        description="Meal name (e.g., 'Breakfast', 'Lunch', 'Dinner', 'Snacks')",
    )
    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today if not specified.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    quantity: float = Field(
        default=1.0,
        description=(
            "Quantity to log. Interpretation depends on the food's serving unit:\n"
            "- For gram-based foods (serving unit = 'g', 'gramo', etc.), pass the actual "
            "weight in grams (e.g. 120 for 120 g of salmon, 200 for 200 g of mango).\n"
            "- For serving-based foods, pass the number of servings (e.g. 1.5).\n"
            "- For placeholder / meal-plan entries not yet consumed, pass a small value "
            "such as 0.1 or 1 so the item appears in the diary with near-zero calories "
            "and can be updated with the real weight after the meal.\n"
            "Minimum: any positive value (> 0). Maximum: 10 000 (covers the largest "
            "realistic single-food gram weight)."
        ),
        gt=0,
        le=10000,
    )
    unit: Optional[str] = Field(
        default=None,
        description="Unit/serving size description (e.g., '1 cup', '100g'). If not provided, uses default serving size from food item.",
    )


class CreateCustomFoodInput(BaseModel):
    """Input model for creating a private custom food."""

    model_config = ConfigDict(str_strip_whitespace=True)

    description: str = Field(
        ..., description="Food name as it appears in MFP", min_length=1, max_length=200
    )
    brand_name: str = Field(
        default="Generic",
        description="Brand. Packaged food = label brand; restaurant = venue name; homemade = 'Generic'.",
        max_length=200,
    )
    serving_amount: float = Field(
        default=100, description="Serving size number (e.g. 100 for '100 g')", gt=0
    )
    serving_unit: str = Field(
        default="g", description="Serving unit (e.g. 'g', 'ml', 'piece', 'box (100 g)')"
    )
    calories: float = Field(..., description="Calories per serving", ge=0)

    carbs: Optional[float] = Field(
        default=None,
        description="NET carbs in g (MFP adds fiber itself to report total; never pre-add fiber)",
        ge=0,
    )
    fiber: Optional[float] = Field(default=None, description="Fiber, g", ge=0)
    sugar: Optional[float] = Field(default=None, description="Sugars, g", ge=0)
    protein: Optional[float] = Field(default=None, description="Protein, g", ge=0)
    fat: Optional[float] = Field(default=None, description="Total fat, g", ge=0)
    saturated_fat: Optional[float] = Field(default=None, description="Saturated fat, g", ge=0)
    polyunsaturated_fat: Optional[float] = Field(default=None, description="Polyunsaturated fat, g", ge=0)
    monounsaturated_fat: Optional[float] = Field(default=None, description="Monounsaturated fat, g", ge=0)
    trans_fat: Optional[float] = Field(default=None, description="Trans fat, g", ge=0)
    cholesterol: Optional[float] = Field(default=None, description="Cholesterol, mg", ge=0)
    sodium: Optional[float] = Field(default=None, description="Sodium, mg", ge=0)
    potassium: Optional[float] = Field(default=None, description="Potassium, mg", ge=0)
    vitamin_a: Optional[float] = Field(default=None, description="Vitamin A, %DV", ge=0)
    vitamin_c: Optional[float] = Field(default=None, description="Vitamin C, %DV", ge=0)
    calcium: Optional[float] = Field(default=None, description="Calcium, %DV", ge=0)
    iron: Optional[float] = Field(default=None, description="Iron, %DV", ge=0)

    country_code: str = Field(
        default="NL",
        description=(
            "Label convention, and it changes carb meaning. 'NL'/EU: `carbs` is read as NET "
            "(MFP reports total = carbs + fiber). Omitting/US: `carbs` is read as TOTAL. "
            "Keep 'NL' unless deliberately entering a US-style total-carb label."
        ),
        min_length=2, max_length=2,
    )
    public: bool = Field(
        default=False, description="Share publicly. Keep False for personal entries."
    )
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class DeleteCustomFoodInput(BaseModel):
    """Input model for deleting a custom food."""

    model_config = ConfigDict(str_strip_whitespace=True)

    food_id: str = Field(..., description="Food id (from mfp_create_custom_food or mfp_list_own_foods)", min_length=1)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class ListOwnFoodsInput(BaseModel):
    """Input model for listing the user's own custom foods."""

    model_config = ConfigDict(str_strip_whitespace=True)

    search: str = Field(default="", description="Optional substring filter on the food name")
    limit: int = Field(default=25, description="Max foods to return", gt=0, le=200)
    response_format: ResponseFormat = Field(
        default=ResponseFormat.MARKDOWN,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class ListRecipesInput(BaseModel):
    """Input model for listing the user's recipes."""

    model_config = ConfigDict(str_strip_whitespace=True)

    search: str = Field(default="", description="Optional words to match in the recipe name")
    response_format: ResponseFormat = Field(
        default=ResponseFormat.JSON,
        description="Output format: 'markdown' for human-readable or 'json' for structured data",
    )


class RemoveFoodFromDiaryInput(BaseModel):
    """Input model for removing food entries from diary."""

    model_config = ConfigDict(extra="forbid")

    date: Optional[str] = Field(
        default=None,
        description="Date in YYYY-MM-DD format. Defaults to today.",
        pattern=r"^\d{4}-\d{2}-\d{2}$",
    )
    entry_id: Optional[str] = Field(
        default=None,
        description=(
            "Specific food_entry_id to remove. If omitted, name_contains is "
            "used to match by name."
        ),
    )
    name_contains: Optional[str] = Field(
        default=None,
        description=(
            "Case-insensitive substring to match against entry names "
            "(e.g. 'banana' or '0.5 cup rice'). Ignored if entry_id is set."
        ),
    )
    meal: Optional[str] = Field(
        default=None,
        description=(
            "Restrict matching to a meal: Breakfast, Lunch, Dinner, Snacks."
        ),
    )
    max_matches: int = Field(
        default=1,
        gt=0,
        le=50,
        description=(
            "Safety cap on how many matching entries to delete in one call."
        ),
    )


# ============================================================================
# Diary Entry Creation Helper Functions
# ============================================================================


def _mfp_api_headers(client, json_body: bool = False) -> Dict[str, str]:
    """
    Build auth headers for MyFitnessPal's v2 JSON API.

    The v2 API backs the current MFP web client. It requires the session's
    OAuth bearer token plus an mfp-client-id identifying the calling client.
    """
    headers = {
        "Authorization": f"Bearer {client.access_token}",
        "mfp-client-id": MFP_CLIENT_ID,
        "mfp-user-id": str(client.user_id),
        "Accept": "application/json",
    }
    if json_body:
        headers["Content-Type"] = "application/json"
    return headers


def get_food_v2(client, mfp_id: str) -> Dict[str, Any]:
    """
    Fetch a food's full v2 record, including its version and serving sizes.

    The diary API rejects entries whose food version does not match the
    current stored version, so this must be read fresh rather than cached.

    Args:
        client: Authenticated myfitnesspal.Client instance
        mfp_id: MyFitnessPal food item ID

    Returns:
        The food object as returned by the v2 API

    Raises:
        RuntimeError: If the food cannot be retrieved
    """
    response = client.session.get(
        f"{MFP_API_BASE}/v2/foods",
        params={"ids": str(mfp_id)},
        headers=_mfp_api_headers(client),
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Could not look up food {mfp_id}: HTTP {response.status_code}"
        )

    items = response.json().get("items") or []
    if not items:
        raise RuntimeError(f"No food found with ID {mfp_id}")
    return items[0]


def search_foods_v2(client, query: str, limit: int) -> List[Dict[str, Any]]:
    """
    Search MFP's food database through the v2 API.

    Replaces the library's HTML search, which returns an empty result page
    when called from datacenter IPs (e.g. Railway).

    Returns:
        List of {"name", "brand", "serving", "calories", "mfp_id"} dicts,
        calories per the first listed serving.
    """
    response = client.session.get(
        f"{MFP_API_BASE}/v2/search/nutrition",
        params={"q": query, "max_items": limit},
        headers=_mfp_api_headers(client),
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(f"Food search failed: HTTP {response.status_code}")
    results = []
    for entry in response.json().get("items") or []:
        food = entry.get("item") or {}
        if not food.get("id"):
            continue
        servings = food.get("serving_sizes") or [{}]
        first = servings[0]
        energy = (food.get("nutritional_contents") or {}).get("energy") or {}
        calories = energy.get("value")
        if calories is not None and first.get("nutrition_multiplier") is not None:
            calories = round(calories * first["nutrition_multiplier"], 1)
        results.append({
            "name": food.get("description", ""),
            "brand": food.get("brand_name") or "",
            "serving": f"{first.get('value', '')} {first.get('unit', '')}".strip(),
            "calories": calories,
            "mfp_id": str(food["id"]),
        })
    return results[:limit]


def select_serving_size(food: Dict[str, Any], unit: Optional[str] = None) -> Dict[str, Any]:
    """
    Choose which of a food's serving sizes to log against.

    Args:
        food: Food object from get_food_v2
        unit: Optional unit to match (e.g. "oz", "medium breast"). Matching is
            case-insensitive and accepts a substring. Falls back to the food's
            default (first) serving size when omitted or unmatched.

    Returns:
        The serving size dict, trimmed to the fields the diary API permits

    Raises:
        RuntimeError: If the food declares no serving sizes
    """
    serving_sizes = food.get("serving_sizes") or []
    if not serving_sizes:
        raise RuntimeError(f"Food {food.get('id')} has no serving sizes")

    chosen = serving_sizes[0]
    if unit:
        wanted = unit.strip().lower()
        exact = [s for s in serving_sizes if str(s.get("unit", "")).lower() == wanted]
        substring = [
            s for s in serving_sizes if wanted in str(s.get("unit", "")).lower()
        ]
        candidates = exact or substring
        if candidates:
            # Some foods list the same unit twice (e.g. a "100 g" pack
            # alongside a per-gram entry) - prefer value=1.0 so quantity
            # means a literal count of `unit`, not a count of packs.
            chosen = next((c for c in candidates if c.get("value") == 1.0), candidates[0])
        else:
            logger.warning(
                f"Unit {unit!r} not found for food {food.get('id')}; "
                f"using default serving {chosen.get('unit')!r}"
            )

    # The diary endpoint rejects any serving_size field beyond these three.
    return {
        "value": chosen["value"],
        "unit": chosen["unit"],
        "nutrition_multiplier": chosen["nutrition_multiplier"],
    }



# ============================================================================
# Custom-food creation (web BFF /api/services/foods)
#
# MFP exposes no custom-food create on the v2 OAuth API, but its own web client
# does it with three plain HTTP calls, all cookie-authenticated. Since this
# server already holds a logged-in cookie jar, it can call them directly — no
# browser automation, no Chrome running.
#
#   GET  /api/auth/csrf                  -> { csrfToken }
#   GET  /api/services/users/foods/mine  -> [ ...foods ]   (source of user_id)
#   POST /api/services/foods             -> { item: {...} } (201/200)
#   DELETE /api/services/foods/{id}      -> 204
#
# CARBS + COUNTRY_CODE (verified 2026-07-26, both directions):
# `country_code` decides how MFP reads nutritional_contents.carbohydrates.
#   country_code="NL" (EU labels exclude fibre):
#       sent value = NET  -> stores net_carbs=sent, carbohydrates=sent+fiber
#   country_code omitted (US default, labels include fibre):
#       sent value = TOTAL -> stores carbohydrates=sent, net_carbs=sent-fiber
# Sending carbohydrates=42, fiber=8 gives 50/42 with "NL" and 42/34 without.
# So country_code is NOT cosmetic: omitting it silently shifts every carb value
# by the fibre amount. Default "NL" and pass NET carbs.
# ============================================================================

MFP_WEB_BASE = "https://www.myfitnesspal.com"

# canonical arg -> MFP nutritional_contents key
_NUTRIENT_KEYS = {
    "fat": "fat",
    "saturated_fat": "saturated_fat",
    "polyunsaturated_fat": "polyunsaturated_fat",
    "monounsaturated_fat": "monounsaturated_fat",
    "trans_fat": "trans_fat",
    "cholesterol": "cholesterol",
    "sodium": "sodium",
    "potassium": "potassium",
    "carbs": "carbohydrates",
    "fiber": "fiber",
    "sugar": "sugar",
    "protein": "protein",
    "vitamin_a": "vitamin_a",
    "vitamin_c": "vitamin_c",
    "calcium": "calcium",
    "iron": "iron",
}


def _web_headers(csrf: Optional[str] = None, json_body: bool = False) -> Dict[str, str]:
    """Headers for the cookie-authenticated web BFF."""
    headers = {"Accept": "application/json"}
    if json_body:
        headers["Content-Type"] = "application/json"
    if csrf:
        headers["x-csrf-token"] = csrf
    return headers


def _api_error_detail(response) -> str:
    """Pull MFP's structured error text out of a failed response.

    Mirrors how add_food_to_diary surfaces v2 errors, so we report the API's own
    message rather than echoing an arbitrary slice of the response body.
    """
    try:
        body = response.json()
    except Exception:
        return ""
    if not isinstance(body, dict):
        return ""
    return str(
        body.get("error_description")
        or body.get("error_details", {}).get("item_error")
        or body.get("error")
        or ""
    )


def _get_csrf_token(client) -> Optional[str]:
    """Fetch a CSRF token. Returns None if unavailable; POST will then 403."""
    try:
        r = client.session.get(
            f"{MFP_WEB_BASE}/api/auth/csrf", headers=_web_headers(), timeout=30
        )
        if r.status_code == 200:
            return r.json().get("csrfToken")
    except Exception as e:
        logger.warning(f"Could not fetch CSRF token: {e}")
    return None


def list_own_foods(client, search: str = "") -> List[Dict[str, Any]]:
    """List the user's own custom foods (newest first), optionally filtered."""
    r = client.session.get(
        f"{MFP_WEB_BASE}/api/services/users/foods/mine",
        params={"search": search},
        headers=_web_headers(),
        timeout=30,
    )
    if r.status_code != 200:
        raise RuntimeError(
            f"Could not list own foods: HTTP {r.status_code}. "
            "The stored session may have expired."
        )
    return r.json() or []


# Recipes are logged as a diary food of type "recipe" whose id differs from
# the recipe id. No endpoint maps one to the other; the only link is that
# they share a `version`, and the food exists only once the recipe has been
# logged (in the MFP app). So the map is built by scanning diary history.
RECIPE_SCAN_DAYS = 120
RECIPE_RESCAN_INTERVAL = 600  # seconds; bounds rescans triggered by misses
_recipe_food_ids: Dict[str, str] = {}
_recipe_scan_at: Optional[float] = None
_recipe_scan_lock = threading.Lock()


def list_recipes(client) -> List[Dict[str, Any]]:
    """The user's recipe box, with nutrition per serving."""
    response = client.session.get(
        f"{MFP_API_BASE}/v2/recipes", headers=_mfp_api_headers(client), timeout=30
    )
    if response.status_code != 200:
        raise RuntimeError(f"Could not list recipes: HTTP {response.status_code}")
    recipes = []
    for r in response.json().get("items") or []:
        servings = r.get("servings") or 1
        total = r.get("nutritional_contents") or {}

        def per_serving(value):
            return round(value / servings, 1) if isinstance(value, (int, float)) else None

        recipes.append({
            "recipe_id": r["id"],
            "name": " ".join((r.get("name") or "").split()),
            "version": r.get("version"),
            "servings": servings,
            "per_serving": {
                "calories": per_serving((total.get("energy") or {}).get("value")),
                "protein": per_serving(total.get("protein")),
                "carbs": per_serving(total.get("carbohydrates")),
                "fat": per_serving(total.get("fat")),
                "fiber": per_serving(total.get("fiber")),
            },
        })
    return recipes


def _scan_recipe_foods(client, days: int = RECIPE_SCAN_DAYS) -> Dict[str, str]:
    """Map recipe version -> diary food id from the last `days` of diary."""
    from concurrent.futures import ThreadPoolExecutor

    def day_foods(day: date) -> List[Dict[str, Any]]:
        response = client.session.get(
            f"{MFP_API_BASE}/v2/diary",
            params={"entry_date": day.strftime("%Y-%m-%d"), "types": "food_entry"},
            headers=_mfp_api_headers(client),
            timeout=30,
        )
        if response.status_code != 200:
            return []
        return [i.get("food") or {} for i in response.json().get("items") or []]

    today = date.today()
    found: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=8) as pool:
        # Newest day first, so setdefault keeps the most recent food per version
        for foods in pool.map(day_foods, [today - timedelta(days=n) for n in range(days)]):
            for f in foods:
                if f.get("type") == "recipe" and f.get("version") and f.get("id"):
                    found.setdefault(str(f["version"]), str(f["id"]))
    logger.info(f"Recipe scan over {days} days found {len(found)} loggable recipes")
    return found


def recipe_food_ids(client, rescan: bool = False) -> Dict[str, str]:
    """
    Map recipe version -> loggable food id, scanned once and cached.

    rescan=True scans again (for a recipe first logged in the app since the
    last scan), at most once per RECIPE_RESCAN_INTERVAL.
    """
    global _recipe_scan_at
    with _recipe_scan_lock:
        due = _recipe_scan_at is None or (
            rescan and time.time() - _recipe_scan_at > RECIPE_RESCAN_INTERVAL
        )
        if due:
            _recipe_food_ids.update(_scan_recipe_foods(client))
            _recipe_scan_at = time.time()
        return dict(_recipe_food_ids)


def _name_matches(query: str, name: str) -> bool:
    """True if every word of the query appears in the name (case-insensitive)."""
    name = name.lower()
    return all(word in name for word in query.lower().split())


def _serving_sizes(amount: float, unit: str) -> List[Dict[str, Any]]:
    """Primary serving plus the container wrapper MFP's own client sends."""
    return [
        {
            "value": amount,
            "unit": unit,
            "nutrition_multiplier": 1,
            "gram_weight": 1,
            "fraction": False,
            "index": 0,
        },
        {
            "value": 1,
            "unit": f"container ({amount} {unit} ea.)",
            "nutrition_multiplier": 1,
            "gram_weight": 1,
            "fraction": False,
            "index": 1,
        },
    ]


def create_custom_food(client, spec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a private custom food via the web BFF.

    Args:
        client: authenticated myfitnesspal.Client
        spec: validated CreateCustomFoodInput as a dict

    Returns:
        {"id": str|None, "status": int, "description": str}
    """
    csrf = _get_csrf_token(client)

    nutrition: Dict[str, Any] = {
        "energy": {"unit": "calories", "value": spec["calories"]},
        "grams": 1,
    }
    for arg, api_key in _NUTRIENT_KEYS.items():
        value = spec.get(arg)
        if value is not None:
            nutrition[api_key] = value

    item = {
        "description": spec["description"],
        "brand_name": spec.get("brand_name") or "Generic",
        "public": bool(spec.get("public", False)),
        "type": "food",
        "nutritional_contents": nutrition,
        "serving_sizes": _serving_sizes(
            spec.get("serving_amount", 100), spec.get("serving_unit", "g")
        ),
    }
    item["country_code"] = spec.get("country_code") or "NL"
    # Ownership is assigned by MyFitnessPal from the session, so user_id is not
    # sent: posting without it returns 200 with the correct owner. Sending it
    # would also mean an extra request, and would fail for an account that has
    # no custom foods yet.

    r = client.session.post(
        f"{MFP_WEB_BASE}/api/services/foods",
        headers=_web_headers(csrf, json_body=True),
        data=json.dumps({"item": item}),
        timeout=30,
    )

    if r.status_code not in (200, 201):
        hint = "" if csrf else " (no CSRF token acquired)"
        detail = _api_error_detail(r)
        raise RuntimeError(
            f"Failed to create custom food: HTTP {r.status_code}{hint}"
            + (f" - {detail}" if detail else "")
        )

    # MFP returns a bare list of the created food object(s); older docs/clients
    # assumed {"item": {...}}. Accept both.
    new_id = None
    try:
        body = r.json()
        if isinstance(body, list) and body:
            new_id = body[0].get("id")
        elif isinstance(body, dict):
            new_id = (body.get("item") or body).get("id")
    except Exception:
        pass
    if new_id is None:
        logger.warning("Food created but MyFitnessPal returned no id")

    logger.info(f"Created custom food {new_id}: {spec['description']}")
    return {"id": new_id, "status": r.status_code, "description": spec["description"]}


def delete_custom_food(client, food_id: str) -> int:
    """Delete a custom food by id. MFP has no update endpoint — recreate + delete."""
    csrf = _get_csrf_token(client)
    r = client.session.delete(
        f"{MFP_WEB_BASE}/api/services/foods/{food_id}",
        headers=_web_headers(csrf),
        timeout=30,
    )
    if r.status_code not in (200, 204):
        detail = _api_error_detail(r)
        raise RuntimeError(
            f"Failed to delete custom food {food_id}: HTTP {r.status_code}"
            + (f" - {detail}" if detail else "")
        )
    logger.info(f"Deleted custom food {food_id}")
    return r.status_code


def add_food_to_diary(
    client, mfp_id: str, meal: str, target_date: date, quantity: float = 1.0, unit: Optional[str] = None
) -> Optional[str]:
    """
    Add a food item to the diary for a specific date and meal.

    Args:
        client: Authenticated myfitnesspal.Client instance
        mfp_id: MyFitnessPal food item ID
        meal: Meal name (Breakfast, Lunch, Dinner, Snacks)
        target_date: Date to add the food entry
        quantity: Number of servings (default 1.0)
        unit: Optional serving unit to log against (e.g. "oz")

    Returns:
        The new entry's UUID, or None if MFP did not return one

    Raises:
        RuntimeError: If the operation fails
    """
    food = get_food_v2(client, mfp_id)
    serving_size = select_serving_size(food, unit)

    meal_name = meal.strip().capitalize()
    if meal_name not in VALID_MEALS:
        raise RuntimeError(
            f"Invalid meal {meal!r}. Expected one of: {', '.join(VALID_MEALS)}"
        )

    entry = {
        "type": "food_entry",
        "date": target_date.strftime("%Y-%m-%d"),
        "meal_name": meal_name,
        "servings": float(quantity),
        "food": {"id": str(food["id"]), "version": str(food["version"])},
        "serving_size": serving_size,
    }

    response = client.session.post(
        f"{MFP_API_BASE}/v2/diary",
        headers=_mfp_api_headers(client, json_body=True),
        data=json.dumps({"items": [entry]}),
        timeout=30,
    )

    if response.status_code not in (200, 201):
        detail = ""
        try:
            body = response.json()
            detail = body.get("error_details", {}).get("item_error") or body.get(
                "error_description", ""
            )
        except Exception:
            pass
        raise RuntimeError(
            f"Failed to add food to diary: HTTP {response.status_code}"
            + (f" - {detail}" if detail else "")
        )

    logger.info(
        f"Added food {mfp_id} ({serving_size['value']} {serving_size['unit']} "
        f"x{quantity}) to {meal_name} for {target_date}"
    )

    try:
        return response.json()["items"][0]["id"]
    except (ValueError, KeyError, IndexError):
        logger.warning("Entry created but MyFitnessPal returned no entry id")
        return None


def _food_entries(client, target_date: date) -> List[Dict[str, Any]]:
    """The day's food entries from the v2 diary API, in display order."""
    date_str = target_date.strftime("%Y-%m-%d")
    response = client.session.get(
        f"{MFP_API_BASE}/v2/diary",
        params={"entry_date": date_str, "types": "food_entry"},
        headers=_mfp_api_headers(client),
        timeout=30,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"Could not read the diary for {date_str}: HTTP {response.status_code}"
        )
    items = [
        i for i in response.json().get("items") or []
        if i.get("type") == "food_entry" and not i.get("deleted")
    ]
    items.sort(key=lambda i: (VALID_MEALS.index(i["meal_name"])
                              if i.get("meal_name") in VALID_MEALS else len(VALID_MEALS),
                              i.get("meal_position", 0)))
    return items


def list_diary_entries(client, target_date: date) -> List[Dict[str, Any]]:
    """
    List the day's food entries, including entries logged in the MFP app or
    website.

    Returns:
        List of {"entry_id", "name", "brand", "meal", "servings", "unit",
        "calories"} dicts in display order. `entry_id` is the v2 UUID that
        `remove_diary_entry_v2` takes.
    """
    return [
        {
            "entry_id": i["id"],
            "name": (i.get("food") or {}).get("description", ""),
            "brand": (i.get("food") or {}).get("brand_name"),
            "meal": i.get("meal_name", ""),
            "servings": i.get("servings"),
            "unit": (i.get("serving_size") or {}).get("unit"),
            "calories": ((i.get("nutritional_contents") or {}).get("energy") or {}).get("value"),
        }
        for i in _food_entries(client, target_date)
    ]


DIARY_NUTRIENTS = (
    "carbohydrates", "fiber", "sugar", "protein", "fat", "saturated_fat",
    "sodium", "cholesterol", "potassium",
)


def _nutrients(contents: Dict[str, Any]) -> Dict[str, Optional[float]]:
    """Calories plus DIARY_NUTRIENTS from a v2 nutritional_contents dict."""
    energy = (contents.get("energy") or {}).get("value")
    out = {"calories": round(energy, 1) if isinstance(energy, (int, float)) else None}
    for key in DIARY_NUTRIENTS:
        value = contents.get(key)
        out[key] = round(value, 1) if isinstance(value, (int, float)) else None
    return out


def _sum_nutrients(rows) -> Dict[str, float]:
    total: Dict[str, float] = {}
    for row in rows:
        for key, value in row.items():
            if value is not None:
                total[key] = round(total.get(key, 0) + value, 1)
    return total


def get_daily_goals(client, target_date: date) -> Optional[Dict[str, Optional[float]]]:
    """The nutrient goals in force on a date, or None if MFP has none for it.

    /v2/nutrient-goals returns rules with a validity window, each holding
    one goal set per weekday.
    """
    response = client.session.get(
        f"{MFP_API_BASE}/v2/nutrient-goals",
        params={"date": target_date.strftime("%Y-%m-%d")},
        headers=_mfp_api_headers(client),
        timeout=30,
    )
    if response.status_code != 200:
        logger.warning(f"Could not read nutrient goals: HTTP {response.status_code}")
        return None
    day = target_date.strftime("%Y-%m-%d")
    weekday = target_date.strftime("%A").lower()
    for rule in response.json().get("items") or []:
        if rule.get("valid_from", "") <= day <= rule.get("valid_to", "9999-12-31"):
            for goal in rule.get("daily_goals") or []:
                if goal.get("day_of_week") == weekday:
                    return _nutrients(goal)
    return None


def get_diary_v2(client, target_date: date) -> Dict[str, Any]:
    """The day's food diary: entries by meal, totals, goals and remaining."""
    meals: Dict[str, Dict[str, Any]] = {m: {"entries": []} for m in VALID_MEALS}
    for i in _food_entries(client, target_date):
        food = i.get("food") or {}
        serving = i.get("serving_size") or {}
        servings = round(i.get("servings") or 0, 3)
        amount = round(servings * (serving.get("value") or 1), 2)
        meal = i.get("meal_name") or "Other"
        meals.setdefault(meal, {"entries": []})["entries"].append({
            "entry_id": i["id"],
            "name": food.get("description"),
            "brand": food.get("brand_name") or None,
            "amount": f"{amount:g} {serving.get('unit', '')}".strip(),
            "servings": servings,
            "nutrition": _nutrients(i.get("nutritional_contents") or {}),
        })
    for m in meals.values():
        m["totals"] = _sum_nutrients(e["nutrition"] for e in m["entries"])
    totals = _sum_nutrients(m["totals"] for m in meals.values())
    goals = get_daily_goals(client, target_date)
    remaining = (
        {k: round(v - totals.get(k, 0), 1) for k, v in goals.items() if v is not None}
        if goals else None
    )
    return {
        "date": str(target_date),
        "meals": meals,
        "daily_totals": totals,
        "daily_goals": goals,
        "remaining": remaining,
    }


def remove_diary_entry_v2(client, entry_id: str) -> None:
    """
    Delete a food diary entry by the UUID the v2 diary API assigned it
    (what `add_food_to_diary` returns and `list_diary_entries` lists).
    """
    response = client.session.delete(
        f"{MFP_API_BASE}/v2/diary/{entry_id}",
        headers=_mfp_api_headers(client),
        timeout=30,
    )
    if response.status_code in (200, 204):
        logger.info(f"Removed diary entry {entry_id}")
        return
    raise RuntimeError(
        f"Remove failed for entry {entry_id}: HTTP {response.status_code}"
    )


# ============================================================================
# MCP Tools
# ============================================================================


@mcp.tool(
    name="mfp_get_diary",
    annotations={
        "title": "Get Food Diary",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_diary(params: GetDiaryInput) -> str:
    """
    Get the food diary for a specific date including all meals and their nutritional information.

    Returns meals (Breakfast, Lunch, Dinner, Snacks) with each food entry's
    entry_id (for mfp_remove_food_from_diary), name, brand, amount, servings and
    nutrition; per-meal and daily totals; the day's goals; and what remains of them.

    Args:
        params: GetDiaryInput containing:
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Formatted diary data with meals, entries, nutrition, and goals
    """
    try:
        client = get_mfp_client()
        target_date = parse_date(params.date)
        data = await asyncio.to_thread(get_diary_v2, client, target_date)
        return format_response(
            data, params.response_format, f"Food Diary for {target_date}"
        )

    except Exception as e:
        return f"Error retrieving diary: {str(e)}"


def _personal_matches(client, query: str) -> List[Dict[str, Any]]:
    """The user's recipes and custom foods whose names match the query.

    Both are private, so MFP's database search never returns them.
    """
    results = []
    try:
        matched = [r for r in list_recipes(client) if _name_matches(query, r["name"])]
        food_ids = recipe_food_ids(client)
        if any(str(r["version"]) not in food_ids for r in matched):
            # May have been logged in the app since the last scan
            food_ids = recipe_food_ids(client, rescan=True)
        for r in matched:
            results.append({
                "name": r["name"],
                "brand": "My recipe",
                "serving": "1 serving",
                "calories": r["per_serving"]["calories"],
                "mfp_id": food_ids.get(str(r["version"])),
                "source": "recipe",
            })
    except Exception as e:
        logger.warning(f"Recipe matching failed: {e}")
    try:
        for f in list_own_foods(client):
            if _name_matches(query, f.get("description") or ""):
                serving = (f.get("serving_sizes") or [{}])[0]
                results.append({
                    "name": f.get("description"),
                    "brand": f.get("brand_name") or "",
                    "serving": f"{serving.get('value', '')} {serving.get('unit', '')}".strip(),
                    "calories": ((f.get("nutritional_contents") or {}).get("energy") or {}).get("value"),
                    "mfp_id": str(f.get("id")),
                    "source": "custom",
                })
    except Exception as e:
        logger.warning(f"Custom food matching failed: {e}")
    return results


@mcp.tool(
    name="mfp_search_food",
    annotations={
        "title": "Search Food Database",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_search_food(params: SearchFoodInput) -> str:
    """
    Search the MyFitnessPal food database for food items.

    Returns a list of matching foods with their name, brand, serving size,
    calories, MFP ID (for mfp_get_food_details and mfp_add_food_to_diary) and
    source. The user's own recipes ("recipe") and custom foods ("custom") that
    match come first; prefer them over database results ("database"). A recipe
    with a null mfp_id has not been logged in the MFP app yet and cannot be
    logged here until it has.

    Args:
        params: SearchFoodInput containing:
            - query (str): Search query (e.g., 'chicken breast')
            - limit (int): Maximum results to return (default 10)
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: List of matching food items with basic nutrition info
    """
    try:
        client = get_mfp_client()
        results = await asyncio.to_thread(_personal_matches, client, params.query)
        for r in search_foods_v2(client, params.query, params.limit):
            results.append({**r, "source": "database"})
        data = {"query": params.query, "count": len(results), "results": results}

        return format_response(
            data, params.response_format, f"Food Search Results for '{params.query}'"
        )

    except Exception as e:
        return f"Error searching foods: {str(e)}"


@mcp.tool(
    name="mfp_get_food_details",
    annotations={
        "title": "Get Food Item Details",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_get_food_details(params: GetFoodDetailsInput) -> str:
    """
    Get detailed nutritional information for a specific food item by its MFP ID.

    Returns complete nutrition breakdown including calories, macros (protein, carbs, fat),
    fiber, sugar, sodium, cholesterol, vitamins, minerals, and available serving sizes.

    Args:
        params: GetFoodDetailsInput containing:
            - mfp_id (str): MyFitnessPal food item ID from search results
            - response_format (str): 'markdown' or 'json'

    Returns:
        str: Complete nutritional information for the food item
    """
    try:
        client = get_mfp_client()
        # client.get_food_item_details() scrapes a now-client-rendered page
        # and raises on nearly every food; get_food_v2 is the reliable path.
        food = get_food_v2(client, params.mfp_id)
        nutrition = food.get("nutritional_contents", {}) or {}
        energy = nutrition.get("energy") or {}

        data = {
            "mfp_id": params.mfp_id,
            "description": food.get("description", "N/A"),
            "brand_name": food.get("brand_name") or None,
            "verified": food.get("verified", False),
            "calories": energy.get("value"),
            # nutrition values below are per this many grams of the food
            "nutrition_basis_grams": nutrition.get("grams"),
            "nutrition": {
                "protein": nutrition.get("protein"),
                "carbohydrates": nutrition.get("carbohydrates"),
                "fat": nutrition.get("fat"),
                "fiber": nutrition.get("fiber"),
                "sugar": nutrition.get("sugar"),
                "sodium": nutrition.get("sodium"),
                "cholesterol": nutrition.get("cholesterol"),
                "saturated_fat": nutrition.get("saturated_fat"),
                "polyunsaturated_fat": nutrition.get("polyunsaturated_fat"),
                "monounsaturated_fat": nutrition.get("monounsaturated_fat"),
                "trans_fat": nutrition.get("trans_fat"),
                "potassium": nutrition.get("potassium"),
                "vitamin_a": nutrition.get("vitamin_a"),
                "vitamin_c": nutrition.get("vitamin_c"),
                "calcium": nutrition.get("calcium"),
                "iron": nutrition.get("iron"),
            },
            "servings": [
                f"{s['value']} {s['unit']}" for s in food.get("serving_sizes", [])
            ],
        }

        return format_response(data, params.response_format, "Food Item Details")

    except Exception as e:
        return f"Error getting food details: {str(e)}"


@mcp.tool(
    name="mfp_add_food_to_diary",
    annotations={
        "title": "Add Food to Diary",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_add_food_to_diary(params: AddFoodToDiaryInput) -> str:
    """
    Add a food item to your MyFitnessPal food diary for a specific date and meal.

    This tool adds a food entry to your diary. Find the food ID (mfp_id) with
    mfp_search_food. It logs any search result the same way: database foods,
    the user's custom foods, and the user's recipes (quantity = servings).
    A recipe whose mfp_id is null cannot be logged until it has been logged
    once in the MFP app.

    Args:
        params: AddFoodToDiaryInput containing:
            - mfp_id (str): MyFitnessPal food item ID (from mfp_search_food)
            - meal (str): Meal name - 'Breakfast', 'Lunch', 'Dinner', or 'Snacks' (default: 'Breakfast')
            - date (str, optional): Date in YYYY-MM-DD format, defaults to today
            - quantity (float): For gram-based foods pass the actual weight in grams (e.g. 120
              for 120 g); for serving-based foods pass the number of servings (e.g. 1.5).
              Use 0.1 or 1 as a placeholder for meals not yet consumed — update to the real
              weight after eating. Range: (0, 10 000].
            - unit (str, optional): Unit/serving size (e.g., '1 cup', '100g')

    Returns:
        str: Confirmation message with details of the added food entry
    """
    try:
        client = get_mfp_client()
        target_date = parse_date(params.date)
        
        # Normalize meal name (capitalize first letter)
        meal = params.meal.strip().capitalize()
        if meal.lower() == "snack":
            meal = "Snacks"
        
        # Add food to diary
        entry_id = add_food_to_diary(
            client=client,
            mfp_id=params.mfp_id,
            meal=meal,
            target_date=target_date,
            quantity=params.quantity,
            unit=params.unit,
        )

        logged_ids = {e["entry_id"] for e in list_diary_entries(client, target_date)}
        if entry_id not in logged_ids:
            raise RuntimeError(
                f"MyFitnessPal accepted the add but entry {entry_id} is not in the "
                f"{target_date} diary"
            )

        try:
            food_name = get_food_v2(client, params.mfp_id).get("description", "Food item")
        except Exception:
            food_name = "Food item"

        return json.dumps(
            {
                "success": True,
                "verified": True,
                "message": f"Successfully added {food_name} to {meal}",
                "entry_id": entry_id,
                "date": str(target_date),
                "meal": meal,
                "food_id": params.mfp_id,
                "food_name": food_name,
                "quantity": params.quantity,
                "unit": params.unit,
            },
            indent=2,
        )
        
    except Exception as e:
        return f"Error adding food to diary: {str(e)}"


def _verify_removed(client, target_date: date, entry_ids: List[str]) -> None:
    remaining = {e["entry_id"] for e in list_diary_entries(client, target_date)}
    still_there = [i for i in entry_ids if i in remaining]
    if still_there:
        raise RuntimeError(
            f"MyFitnessPal accepted the delete but these entries are still in the "
            f"diary: {', '.join(still_there)}"
        )


@mcp.tool(
    name="mfp_remove_food_from_diary",
    annotations={
        "title": "Remove Food From Diary",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_remove_food_from_diary(params: RemoveFoodFromDiaryInput) -> str:
    """
    Remove (delete) one or more food entries from your diary.

    Two modes:

    1. By entry_id (precise): delete exactly the entry whose id matches -
       this is the UUID shown by `mfp_get_diary` or returned by
       `mfp_add_food_to_diary`. Works for entries logged anywhere.

    2. By name_contains (fuzzy): list the day's entries, find ones whose
       name contains the given substring (case-insensitive), optionally
       restricted to a meal, and delete up to max_matches of them.

    Args:
        params: RemoveFoodFromDiaryInput with one of:
            - entry_id: the entry's UUID, as listed by mfp_get_diary or
              returned by mfp_add_food_to_diary
            - name_contains: substring match against entry names
            - meal: restrict matching to one meal
            - max_matches: safety cap for fuzzy matches (default 1)
            - date: date to operate on (default today)

    Re-reads the diary afterwards and fails if any entry is still present.

    Returns:
        JSON describing each entry that was removed.
    """
    try:
        client = get_mfp_client()
        target_date = parse_date(params.date)

        # Mode 1: delete a single entry by its v2 UUID
        if params.entry_id:
            remove_diary_entry_v2(client, params.entry_id)
            _verify_removed(client, target_date, [params.entry_id])
            return json.dumps({
                "success": True,
                "verified": True,
                "removed": [{"entry_id": params.entry_id}],
                "date": str(target_date),
            }, indent=2)

        # Mode 2: fuzzy match by name (+ optional meal filter)
        if not params.name_contains:
            return ("Error removing food: provide either entry_id or "
                    "name_contains")

        entries = list_diary_entries(client, target_date)
        needle = params.name_contains.lower()
        meal_filter = (
            params.meal.lower() if params.meal else None
        )

        matches = []
        for e in entries:
            if needle not in e["name"].lower():
                continue
            if meal_filter and meal_filter not in e["meal"].lower():
                continue
            matches.append(e)

        if not matches:
            return json.dumps({
                "success": False,
                "removed": [],
                "message": (
                    f"No entries matched '{params.name_contains}'"
                    + (f" in {params.meal}" if params.meal else "")
                ),
            }, indent=2)

        to_remove = matches[: params.max_matches]
        removed = []
        for e in to_remove:
            remove_diary_entry_v2(client, e["entry_id"])
            removed.append({
                "entry_id": e["entry_id"],
                "name": e["name"],
                "meal": e["meal"],
            })
        _verify_removed(client, target_date, [e["entry_id"] for e in removed])

        return json.dumps({
            "success": True,
            "verified": True,
            "removed": removed,
            "matched_count": len(matches),
            "remaining_matches_skipped": max(
                0, len(matches) - len(to_remove)
            ),
            "date": str(target_date),
        }, indent=2, ensure_ascii=False)

    except Exception as e:
        return f"Error removing food: {e}"


# ============================================================================
# Cookie refresh from a local browser (scripts/refresh_local_cookies.py)
# ============================================================================


def _verify_cookies_and_format(cookies: Dict[str, str], source: str) -> str:
    """Verify cookies via a live MFP round-trip, then persist on success.

    Persisting only after verification matches the auto-discovery path's
    anti-poisoning behavior — a stale/expired session can't clobber a
    previously good `cookies.json`.
    """
    if not _has_real_mfp_session(cookies):
        return (
            f"No MyFitnessPal session token found in {source}. "
            "Make sure you are logged into myfitnesspal.com in that browser, "
            "then try again."
        )
    try:
        import myfitnesspal
        cookiejar = dict_to_cookiejar(cookies)
        myfitnesspal.Client(cookiejar=cookiejar)  # construction validates the session
    except Exception as e:
        return (
            f"Cookies were extracted from {source} but verification failed: "
            f"{e}. The session may have expired — log in again and retry. "
            f"(cookies.json was NOT overwritten.)"
        )
    save_cookies(cookies)
    return (
        f"Successfully extracted and verified {len(cookies)} cookies "
        f"from {source}. Authentication is now working."
    )


def refresh_browser_cookies(browser: str = "auto") -> str:
    """
    Extract, verify and save session cookies from a local web browser.

    Not an MCP tool: it needs a browser on the machine running the server.
    Run it via scripts/refresh_local_cookies.py. You must be logged into
    myfitnesspal.com in the target browser.

    Args:
        browser: Source to extract cookies from. Options:
                 - 'auto' (default): scan every installed Chromium-based
                   browser on macOS (Arc, Chrome, Edge, Brave, Vivaldi,
                   Opera, ...) and use the first one with a valid session.
                 - 'arc', 'chrome', 'chromium', 'edge', 'brave', 'vivaldi',
                   'opera': force a specific Chromium browser (macOS).
                 - 'firefox': use browser_cookie3 (Firefox is not Chromium).

    Returns:
        Success message or error description.
    """
    browser_key = browser.lower().strip()

    # 'auto' — discover every Chromium browser via keychain Safe Storage
    if browser_key == "auto":
        result = try_chromium_browsers_for_session_cookies()
        if not result:
            return (
                "Auto-discovery did not find a Chromium browser with a "
                "valid MyFitnessPal session. Log into myfitnesspal.com in "
                "Arc, Chrome, Edge, Brave, Vivaldi, or Opera, then retry. "
                "(macOS only — on Linux/Windows, pass 'chrome' or "
                "'firefox' instead.)"
            )
        browser_name, cookies = result
        return _verify_cookies_and_format(cookies, browser_name)

    # Explicit Chromium browser
    if browser_key in _CHROMIUM_BROWSER_ALIASES:
        canonical = _CHROMIUM_BROWSER_ALIASES[browser_key]
        if sys.platform == "darwin":
            service_name = f"{canonical} Safe Storage"
            cookies = _try_extract_from_chromium_browser(service_name)
            if cookies is None:
                return (
                    f"Could not read cookies from {canonical}. Make sure "
                    "the browser is installed and you have logged in at "
                    "least once."
                )
            return _verify_cookies_and_format(cookies, canonical)
        # Non-macOS: keychain-based path doesn't apply. browser_cookie3
        # handles chrome/chromium on Linux/Windows via their default
        # profile paths; other Chromium browsers aren't supported there.
        if browser_key in ("chrome", "chromium"):
            try:
                import browser_cookie3
                cj = browser_cookie3.chrome(domain_name=".myfitnesspal.com")
                cookies = {c.name: c.value for c in cj}
            except Exception as e:
                return f"Error extracting cookies from {browser_key}: {e}"
            return _verify_cookies_and_format(cookies, browser_key)
        return (
            f"{canonical} cookie extraction requires macOS (keychain-backed "
            f"Safe Storage). On this platform, use 'chrome' or 'firefox'."
        )

    # Firefox via browser_cookie3 (it has its own format, not Chromium)
    if browser_key == "firefox":
        try:
            import browser_cookie3
            cj = browser_cookie3.firefox(domain_name=".myfitnesspal.com")
            cookies = {c.name: c.value for c in cj}
        except Exception as e:
            return f"Error extracting cookies from firefox: {e}"
        return _verify_cookies_and_format(cookies, "firefox")

    valid_options = sorted({*_CHROMIUM_BROWSER_ALIASES, "firefox", "auto"})
    return (
        f"Unsupported browser: {browser!r}. Use 'auto' to scan all installed "
        f"Chromium browsers, or one of: {', '.join(valid_options)}."
    )


# ============================================================================
# Main Entry Point
# ============================================================================



@mcp.tool(
    name="mfp_create_custom_food",
    annotations={
        "title": "Create Custom Food",
        "readOnlyHint": False,
        "destructiveHint": False,
        "idempotentHint": False,
        "openWorldHint": True,
    },
)
async def mfp_create_custom_food(params: CreateCustomFoodInput) -> str:
    """
    Create a private custom food in the user's MyFitnessPal account.

    Fills the full nutrition panel MFP supports (macros, fats breakdown,
    cholesterol, sodium, potassium, fiber, sugars, and the four %DV micros).
    Uses the cookie-authenticated web endpoint, so no browser needs to be
    running. Returns the new food's id, which mfp_add_food_to_diary accepts.

    CARBS ARE NET (with the default country_code="NL"): pass net carbs in
    `carbs`; MFP stores net_carbs as given and reports total = carbs + fiber.
    Never pre-add fiber. Verified: carbs=42/fiber=8 stores 50/42 under "NL" but
    42/34 with country_code omitted, so the field is load-bearing, not cosmetic.

    MFP has no update endpoint. To correct a food, create the corrected version
    then mfp_delete_custom_food the old one.

    Args:
        params: CreateCustomFoodInput (description, brand_name, serving_amount,
            serving_unit, calories + optional nutrients, public, response_format)

    Returns:
        str: The created food's id, description and HTTP status
    """
    try:
        client = get_mfp_client()
        result = create_custom_food(client, params.model_dump())
        return format_response(result, params.response_format, "Custom Food Created")
    except Exception as e:
        return f"Error creating custom food: {str(e)}"


@mcp.tool(
    name="mfp_list_own_foods",
    annotations={
        "title": "List Own Custom Foods",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_list_own_foods(params: ListOwnFoodsInput) -> str:
    """
    List the user's own custom foods, newest first.

    Private custom foods do not reliably surface in mfp_search_food, so this is
    the way to find the id of something previously created.

    Args:
        params: ListOwnFoodsInput (search, limit, response_format)

    Returns:
        str: Matching custom foods with id, description, brand and calories
    """
    try:
        client = get_mfp_client()
        foods = list_own_foods(client, params.search)[: params.limit]
        data = {
            "count": len(foods),
            "foods": [
                {
                    "id": f.get("id"),
                    "description": f.get("description"),
                    "brand_name": f.get("brand_name"),
                    "calories": (f.get("nutritional_contents", {}).get("energy", {}) or {}).get("value"),
                    "serving": (f.get("serving_sizes") or [{}])[0].get("unit"),
                    "public": f.get("public"),
                }
                for f in foods
            ],
        }
        return format_response(data, params.response_format, "My Custom Foods")
    except Exception as e:
        return f"Error listing own foods: {str(e)}"


@mcp.tool(
    name="mfp_delete_custom_food",
    annotations={
        "title": "Delete Custom Food",
        "readOnlyHint": False,
        "destructiveHint": True,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_delete_custom_food(params: DeleteCustomFoodInput) -> str:
    """
    Delete one of the user's custom foods by id.

    Destructive and not recoverable. A food actively referenced by a logged
    diary entry may be refused by MyFitnessPal.

    Args:
        params: DeleteCustomFoodInput (food_id)

    Returns:
        str: Confirmation with the HTTP status
    """
    try:
        client = get_mfp_client()
        status = delete_custom_food(client, params.food_id)
        data = {"food_id": params.food_id, "deleted": True, "status": status}
        return format_response(data, params.response_format, "Custom Food Deleted")
    except Exception as e:
        return f"Error deleting custom food: {str(e)}"


@mcp.tool(
    name="mfp_list_recipes",
    annotations={
        "title": "List Recipes",
        "readOnlyHint": True,
        "destructiveHint": False,
        "idempotentHint": True,
        "openWorldHint": True,
    },
)
async def mfp_list_recipes(params: ListRecipesInput) -> str:
    """
    List the user's saved recipes with nutrition per serving.

    For browsing. To log a recipe, find it with mfp_search_food and pass its
    mfp_id to mfp_add_food_to_diary. `mfp_id` is null for a recipe that has
    not been logged in the MFP app since it was last edited: MFP only creates
    the loggable food on first log, so such a recipe cannot be logged from
    here yet.

    Args:
        params: ListRecipesInput with optional `search` words

    Returns:
        Recipes with recipe_id, name, servings, per_serving nutrition, mfp_id
    """
    def run() -> Dict[str, Any]:
        client = get_mfp_client()
        food_ids = recipe_food_ids(client)
        recipes = [
            {**{k: v for k, v in r.items() if k != "version"},
             "mfp_id": food_ids.get(str(r["version"]))}
            for r in list_recipes(client)
            if _name_matches(params.search, r["name"])
        ]
        return {"count": len(recipes), "recipes": recipes}

    try:
        data = await asyncio.to_thread(run)
        return format_response(data, params.response_format, "My Recipes")
    except Exception as e:
        return f"Error listing recipes: {str(e)}"


@mcp.custom_route("/favicon.ico", methods=["GET"])
async def favicon(request):
    """Serve the icon at the domain root, where favicon fetchers look."""
    from starlette.responses import Response

    return Response(ICON_PNG, media_type="image/png")


SESSION_COOKIE = "__Secure-next-auth.session-token"
KEEPALIVE_INTERVAL = timedelta(hours=6)


def refresh_session() -> None:
    """
    Renew the MFP session and save every cookie MFP sends back.

    Tools authenticate through /user/auth_token, which accepts either the
    legacy `_mfp_session` cookie (re-issued on each call) or the
    `remember_me` login cookies; it does not need the NextAuth session token.
    So this calls /user/auth_token, which is what fails when tools would, and
    then /api/auth/session, which renews the NextAuth token and `remember_me`
    for another 30 days while that session is active. A copy of the NextAuth
    token goes stale once the browser it came from moves on, so its failure
    is only a warning.

    Raises:
        RuntimeError: If /user/auth_token no longer accepts the session
    """
    cookies = load_cookies()
    if not cookies:
        raise RuntimeError("No session cookies to refresh")
    import requests

    session = requests.Session()
    session.cookies = dict_to_cookiejar(cookies)
    response = session.get(
        f"{MFP_WEB_BASE}/user/auth_token?refresh=true", headers=_web_headers(), timeout=30
    )
    try:
        authed = response.status_code == 200 and "access_token" in response.json()
    except ValueError:
        authed = False
    if not authed:
        raise RuntimeError(
            f"MFP no longer accepts the session (auth_token HTTP {response.status_code}); "
            "re-export MFP_COOKIES_JSON"
        )

    expires = None
    try:
        body = session.get(
            f"{MFP_WEB_BASE}/api/auth/session", headers=_web_headers(), timeout=30
        ).json()
        expires = body.get("expires") if body.get("userId") else None
    except Exception as e:
        logger.warning(f"NextAuth session check failed: {e}")

    # Seeded cookies sit on .myfitnesspal.com. A response either replaces one
    # in place or sets a host-only copy on www.myfitnesspal.com, so where a
    # name appears twice the copy on the other domain is the newer one.
    latest: Dict[str, str] = {}
    for c in session.cookies:
        if c.name not in latest or c.domain != ".myfitnesspal.com":
            latest[c.name] = c.value
    save_cookies(latest)
    if expires:
        logger.info(f"Refreshed MFP session; NextAuth session now expires {expires}")
    else:
        logger.warning(
            "Refreshed MFP session; NextAuth session is not active, so only the "
            "legacy session was renewed"
        )


def _keepalive_loop(stop: "threading.Event") -> None:
    while True:
        try:
            refresh_session()
        except Exception as e:
            logger.error(f"MFP session refresh failed: {e}")
        if stop.wait(KEEPALIVE_INTERVAL.total_seconds()):
            return


def _warm_recipe_cache() -> None:
    try:
        recipe_food_ids(get_mfp_client())
    except Exception as e:
        logger.warning(f"Recipe scan at startup failed: {e}")


class _RedactPathSecret(logging.Filter):
    """Keep the URL path secret out of the access log."""

    def __init__(self, secret: str):
        super().__init__()
        self.secret = secret

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                a.replace(self.secret, "<secret>") if isinstance(a, str) else a
                for a in record.args
            )
        return True


def main():
    """Run the MCP server.

    Transport is chosen by MFP_MCP_TRANSPORT: "stdio" (default) or
    "streamable-http". HTTP mode serves at /<MFP_MCP_PATH_SECRET>/mcp on
    $PORT; the unguessable path is the only access control, so the secret
    is required.
    """
    transport = os.environ.get("MFP_MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        secret = os.environ.get("MFP_MCP_PATH_SECRET", "")
        if len(secret) < 32 or not secret.isalnum():
            raise SystemExit(
                "MFP_MCP_PATH_SECRET must be set to an alphanumeric string of at least "
                "32 characters in streamable-http mode."
            )
        logging.getLogger("uvicorn.access").addFilter(_RedactPathSecret(secret))
        threading.Thread(
            target=_keepalive_loop, args=(threading.Event(),), daemon=True
        ).start()
        threading.Thread(target=_warm_recipe_cache, daemon=True).start()
        mcp.settings.host = "0.0.0.0"
        mcp.settings.port = int(os.environ.get("PORT", "8000"))
        mcp.settings.streamable_http_path = f"/{secret}/mcp"
        mcp.settings.stateless_http = True
        # The constructor enabled localhost-only Host checks; the public
        # hostname would fail them.
        mcp.settings.transport_security = None
    elif transport != "stdio":
        raise SystemExit(f"Unsupported MFP_MCP_TRANSPORT: {transport!r}")
    mcp.run(transport=transport)


if __name__ == "__main__":
    main()