# MyFitnessPal MCP Server

A [Model Context Protocol (MCP)](https://modelcontextprotocol.io/) server that enables AI assistants like Claude to interact with your MyFitnessPal data, focused on the food diary: reading it, searching foods (including your recipes and custom foods), logging and removing entries, and managing custom foods.

It runs two ways:

- **Locally over stdio**, for Claude Desktop and Claude Code. It reads your MFP session from your browser. See [Configuration for Claude Desktop](#configuration-for-claude-desktop).
- **Remotely over streamable HTTP**, for claude.ai on the web and in the mobile apps. This fork deploys it on Railway, with the session supplied as an environment variable and the endpoint guarded by a secret URL path. See [Remote Deployment](#remote-deployment-railway-for-claudeai-and-mobile).

The server also sends usage instructions to the client (read the diary before logging, how to correct custom foods, net carbs), so they need not be repeated in your prompts.

## Features

| Tool | Type | Description |
|------|------|-------------|
| `mfp_get_diary` | Read | Get food diary entries for any date |
| `mfp_search_food` | Read | Search your recipes, your custom foods and the MyFitnessPal database |
| `mfp_list_recipes` | Read | List your saved recipes with nutrition per serving |
| `mfp_get_food_details` | Read | Get detailed nutrition info for a food item |
| `mfp_add_food_to_diary` | Write | Log any search result (database food, custom food or recipe) to a meal and date |
| `mfp_remove_food_from_diary` | Write | Remove a logged entry from your diary |
| `mfp_create_custom_food` | Write | Create a private custom food with a full nutrition panel |
| `mfp_list_own_foods` | Read | List your own custom foods, e.g. to find one to delete |
| `mfp_delete_custom_food` | Write | Delete one of your custom foods |

## How it talks to MyFitnessPal

MyFitnessPal has no public API. Everything here goes through MFP's internal JSON APIs, the ones its own web client uses, authenticated with your session: the diary, food search, food details and recipes through the v2 API (`api.myfitnesspal.com/v2`), and custom foods through the website's `/api/services` endpoints. [`python-myfitnesspal`](https://github.com/coddingtonbear/python-myfitnesspal) is used only to open the session (it trades the cookies for the v2 API's bearer token); none of its HTML scraping is used, since MFP has been retiring those pages.

**The diary** (`mfp_get_diary`) reads the day's food entries from `GET /v2/diary?types=food_entry`, groups them by meal, and sums the totals. Goals come from `GET /v2/nutrient-goals`, which returns rules with a validity window and one goal set per weekday; the diary uses the one in force on that date and reports what remains. Water, notes and exercise are not included.

**Custom-food writes** (`mfp_create_custom_food`, `mfp_list_own_foods`,
`mfp_delete_custom_food`) use a different endpoint family: MFP's v2 API exposes no
custom-food create, so these call the same cookie-authenticated web endpoints their
website uses (`/api/auth/csrf`, `/api/services/users/foods/mine`, `/api/services/foods`).
No browser needs to be running — the stored session cookies are sufficient.

**Diary entry ids and deletes** also use the v2 API: `GET /v2/diary?types=food_entry` lists every food entry for a day with its UUID, including entries logged in the MFP app or website, and `DELETE /v2/diary/{uuid}` removes one. `mfp_get_diary` reads the same endpoint, so every entry carries its UUID as `entry_id`. The legacy `/food/remove/{id}` endpoint this server used to call returns success without deleting anything, so it is no longer used.

**Recipes.** `GET /v2/recipes` lists the recipe box. A logged recipe is an ordinary diary entry whose food has `"type": "recipe"` and a different id from the recipe. No endpoint maps a recipe to that food; the two share only a `version`, and MFP creates the food the first time the recipe is logged in the app. So the server scans the last 120 days of diary for recipe foods, matches them to recipes by `version`, and caches the map in memory (built at startup in HTTP mode, rescanned at most every 10 minutes when a recipe is not found). Search returns a recipe's food id as its `mfp_id`, so `mfp_add_food_to_diary` logs recipes like any other food. When a matching recipe has no food id, search rescans first. A recipe never logged in the app, or not logged since it was last edited, cannot be logged here until it has been logged there once.

**Search covers your own foods.** Recipes and custom foods are private, so MFP's database search (`GET /v2/search/nutrition`) never returns them. `mfp_search_food` matches them by name (every query word must appear) and lists them first, tagged `"source": "recipe"` or `"custom"`, ahead of `"database"` results.

**Writes check themselves.** After `mfp_add_food_to_diary` and `mfp_remove_food_from_diary`, the server re-lists the day and returns an error if the entry is not (or is still) there. A successful response carries `"verified": true`.

That interface is undocumented and was determined by observing the web client. It works today,
but MyFitnessPal can change it without notice. They have already done so once: this server
originally posted to `/food/diary/{user}/add`, which now returns 404, leaving food logging
broken. If logging starts failing, that is the most likely cause.

## Prerequisites

- **Python 3.10–3.12** (check with `python3 --version`)

  Not 3.13+: `lxml`, pulled in by `myfitnesspal`, has no wheels for it and fails to build
  against the 3.14 C API. On macOS, `brew install python@3.12`.

- **pip 21.3+** (for pyproject.toml support; upgrade with `pip install --upgrade pip`)
- **MyFitnessPal account**
- **One of the following for authentication:**
  - **Recommended (macOS):** any Chromium-based browser (Arc, Chrome, Edge,
    Brave, Vivaldi, Opera, ...) with an active MyFitnessPal login session —
    the MCP auto-discovers the session on next call
  - Firefox with an active MyFitnessPal login session (via the
    `browser_cookie3` fallback)
  - Legacy: your MFP username/email and password (see caveats below —
    MFP's NextAuth backend rejects the form-POST flow, so credential auth
    only works while cached cookies remain valid)

### Authentication Options

This MCP supports multiple authentication methods:

| Method | Setup | Persistence |
|--------|-------|-------------|
| **Chromium browser auto-discovery (macOS, recommended)** | Log into myfitnesspal.com in any Chromium-based browser (Arc, Chrome, Edge, Brave, Vivaldi, Opera, ...). The MCP auto-detects installed browsers via the macOS keychain and uses whichever one is logged in. | Until browser session expires (cached for 30 days in `~/.mfp_mcp/cookies.json`) |
| **Encrypted credentials (legacy)** | Add encrypted `MFP_USERNAME` and `MFP_PASSWORD` to Claude Desktop config; set `MFP_SECRET_KEY` outside the config (for example via shell env or OS keychain) | Form login no longer works against MFP's NextAuth backend — only useful if cached cookies are still valid |
| **Plain credentials (legacy)** | Add `MFP_USERNAME` and `MFP_PASSWORD` to Claude Desktop config | Same as above — form login flow is deprecated |
| **Browser cookies (browser_cookie3 fallback)** | Log into myfitnesspal.com in Chrome or Firefox via the default profile paths | Until browser session expires |
| **Cookies in `MFP_COOKIES_JSON` (remote)** | Set the variable to the JSON from `scripts/export_cookies.py`. For hosts with no browser. | Renewed by the server indefinitely, if `MFP_STATE_DIR` is persistent (see [Session refresh](#session-refresh)) |

> **Note**: MyFitnessPal migrated their authentication to NextAuth, so the
> legacy form-POST `authenticate_with_credentials` path almost always fails
> for fresh logins. The Chromium auto-discovery path is the reliable way to
> get a session on macOS — just log in via any modern browser and the MCP
> picks it up automatically on the next call.

## Installation

### Option 1: Install from Source (Recommended)

```bash
# Clone the repository
git clone https://github.com/YOUR_USERNAME/myfitnesspal-mcp-python.git
cd myfitnesspal-mcp-python

# Create virtual environment (use python3.10+ on macOS/Linux)
python3 -m venv venv
# On macOS, you may need to specify version: python3.12 -m venv venv

# Activate virtual environment
source venv/bin/activate  # macOS/Linux
# On Windows: .\venv\Scripts\activate

# Upgrade pip (required for pyproject.toml support)
pip install --upgrade pip

# Install the package in editable mode
pip install -e .
```

### Option 2: Install with pip (when published)

```bash
pip install mfp-mcp
```

> **Note**: Option 2 requires the package to be published to PyPI. For now, use Option 1.

### Verify Installation

After installation, verify the server can start:

```bash
# With venv activated
python -m mfp_mcp.server
```

You should see the server waiting for input (it communicates via stdio). Press `Ctrl+C` to stop.

To test authentication (optional):

```bash
MFP_USERNAME="your_email" MFP_PASSWORD="your_password" python -c "
from mfp_mcp.server import get_mfp_client
client = get_mfp_client()
print('Authentication successful!')
"
```

## Configuration for Claude Desktop

### Step 1: Locate Your Config File

| OS | Config File Location |
|----|---------------------|
| **macOS** | `~/Library/Application Support/Claude/claude_desktop_config.json` |
| **Windows** | `%APPDATA%\Claude\claude_desktop_config.json` |

### Step 2: Add the MCP Server Configuration

If the file doesn't exist, create it. Add or merge the following configuration:

#### Option A: With Encrypted Credentials (Enhanced Security)

Encrypt your credentials before storing them in the config file. See [Encrypted Credentials](#encrypted-credentials-enhanced-security) for setup instructions.

> ⚠️ **Security note**: Encryption only provides meaningful protection if `MFP_SECRET_KEY` is stored **separately** from the config file (e.g., set in your shell profile or OS keychain). Storing the key alongside the encrypted values in the same config file means anyone who obtains the config can still decrypt your credentials.

**macOS Example** (with key set separately in your shell environment):
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "/Users/yourname/myfitnesspal-mcp-python/venv/bin/python",
      "args": ["-m", "mfp_mcp.server"],
      "env": {
        "MFP_USERNAME": "gAAAAAB...<encrypted_email>",
        "MFP_PASSWORD": "gAAAAAB...<encrypted_password>"
      }
    }
  }
}
```

#### Option B: With Plain Credentials (No Browser Required)

**macOS Example:**
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "/Users/yourname/myfitnesspal-mcp-python/venv/bin/python",
      "args": ["-m", "mfp_mcp.server"],
      "env": {
        "MFP_USERNAME": "your_email@example.com",
        "MFP_PASSWORD": "your_password"
      }
    }
  }
}
```

**Windows Example:**
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "C:\\Users\\YourName\\myfitnesspal-mcp-python\\venv\\Scripts\\python.exe",
      "args": ["-m", "mfp_mcp.server"],
      "env": {
        "MFP_USERNAME": "your_email@example.com",
        "MFP_PASSWORD": "your_password"
      }
    }
  }
}
```

#### Option C: Without Credentials (Browser Cookie Fallback)

**macOS Example:**
```json
{
  "mcpServers": {
    "myfitnesspal": {
      "command": "/Users/yourname/myfitnesspal-mcp-python/venv/bin/python",
      "args": ["-m", "mfp_mcp.server"]
    }
  }
}
```

> ⚠️ **Important**: Use **full absolute paths** to the Python executable in your virtual environment. Replace `yourname`/`YourName` with your actual username.

### Step 3: Restart Claude Desktop

After saving the config file, **completely quit and restart Claude Desktop** for the changes to take effect.

### Step 4: Verify Connection

In Claude Desktop, you should see a hammer icon (🔨) indicating MCP tools are available. Try asking:

> "Show my MyFitnessPal diary for today"

## Authentication Methods

The MCP server supports four authentication methods, tried in this order:

### 1. Environment Variables (Legacy)
Set `MFP_USERNAME` and `MFP_PASSWORD` in your Claude Desktop config's `env`
section. You can store them as plain text or encrypted (see below).

> ⚠️ **Note**: MyFitnessPal migrated to a NextAuth backend, so the form-POST
> flow this method uses no longer produces a session cookie. Credential auth
> only succeeds while `~/.mfp_mcp/cookies.json` still holds a valid session
> from a previous browser login — after that, this method silently falls
> through to the browser cookie paths below. Prefer the Chromium
> auto-discovery method on macOS.

```json
"env": {
  "MFP_USERNAME": "your_email@example.com",
  "MFP_PASSWORD": "your_password"
}
```

### Encrypted Credentials (Enhanced Security)

Instead of storing plain-text credentials, you can encrypt them using [Fernet symmetric encryption](https://cryptography.io/en/latest/fernet/) from the `cryptography` library. The server decrypts them at runtime using `MFP_SECRET_KEY`.

> ⚠️ **Important**: For encryption to be meaningful, `MFP_SECRET_KEY` must be kept **outside** the Claude Desktop config file. The server resolves it in this order:
> 1. `MFP_SECRET_KEY` environment variable (shell profile, not the Claude config)
> 2. OS keychain — service `mfp-mcp`, account `MFP_SECRET_KEY` **(recommended)**

**Step 1 — Generate and store the key in one command:**

```bash
npm install
npm run store-key
```

`store-key` generates a Fernet-compatible key, stores it in the OS keychain (`mfp-mcp` / `MFP_SECRET_KEY`), and prints the key so you can use it in Step 2. See [Key Management CLI](#key-management-cli) for all available flags.

**Step 2 — Encrypt your credentials:**

```python
from cryptography.fernet import Fernet

key = b"abc123XYZ...=="  # your key from Step 1
f = Fernet(key)

encrypted_user = f.encrypt(b"your_email@example.com").decode()
encrypted_pass = f.encrypt(b"your_password").decode()

print("MFP_USERNAME:", encrypted_user)
print("MFP_PASSWORD:", encrypted_pass)
```

**Step 3 — Add only the encrypted values to your Claude Desktop config:**

```json
"env": {
  "MFP_USERNAME": "gAAAAAB...<encrypted>",
  "MFP_PASSWORD": "gAAAAAB...<encrypted>"
}
```

The key stays in the keychain — it never touches the config file.

**Alternative: shell profile (simpler, still outside the Claude config):**

```bash
# Add to ~/.zshrc or ~/.bashrc — do NOT put this in claude_desktop_config.json
export MFP_SECRET_KEY="abc123XYZ...=="
```

If `MFP_SECRET_KEY` is not found in the environment or keychain, the server treats `MFP_USERNAME` and `MFP_PASSWORD` as plain text (backward compatible).

### 2. Stored Session Cookies
If `MFP_COOKIES_JSON` is set to a JSON object of cookie name → value, those cookies are used and the file below is ignored. This is how remote deployments authenticate; see [Remote Deployment](#remote-deployment-railway-for-claudeai-and-mobile).

Otherwise, after successful authentication, session cookies are saved to `~/.mfp_mcp/cookies.json`. These persist for 30 days, so you won't need to re-authenticate frequently.

### 3. Chromium Browser Auto-Discovery (macOS)

If no credentials are provided and stored cookies are absent or expired, the
server scans the macOS keychain for `<Browser> Safe Storage` entries to find
every installed Chromium-based browser, then tries each one's cookies
database until it finds a valid MyFitnessPal session token.

This works out of the box with Arc, Chrome, Edge, Brave, Vivaldi, Opera,
Chromium, and any other Chromium-derived browser. You only need to be
logged into [myfitnesspal.com](https://www.myfitnesspal.com) in one of
them.

The first successful extraction is persisted to `~/.mfp_mcp/cookies.json`,
so subsequent calls skip the discovery step until the session expires.

To force a re-read, or pick a browser, run:

```bash
uv run python scripts/refresh_local_cookies.py                    # scan everything (default)
uv run python scripts/refresh_local_cookies.py --browser arc      # or chrome, edge, brave, ...
uv run python scripts/refresh_local_cookies.py --browser firefox  # via browser_cookie3
```

### 4. browser_cookie3 Fallback (Legacy)
A final fallback uses [`browser_cookie3`](https://pypi.org/project/browser-cookie3/)
to read Chrome or Firefox cookies from the default profile paths. Useful on
Linux/Windows or if the macOS auto-discovery path can't access your
keychain.

## Security Note on Credentials

Your MyFitnessPal credentials in the Claude Desktop config are stored locally on your machine. The config file is only readable by your user account. Options to harden this further:

### 1. **Encrypt credentials + store the key in the OS keychain**

The strongest option. The ciphertext lives in the config; the key never does. See [Encrypted Credentials](#encrypted-credentials-enhanced-security).

### 2. **Encrypt credentials + export the key in your shell profile**

Still separates key from ciphertext, though the key is on disk.

### 3. Storing `MFP_PASSWORD` in your MCP client

Storing `MFP_PASSWORD` in your MCP client config puts your MyFitnessPal password in **plaintext
on disk**, readable by anything running as your user. It is convenient — the server can
re-authenticate indefinitely — but it is a real tradeoff, not a formality.

### 4. Use browser cookies instead (no credentials stored in config at all)

Prefer browser-cookie auth if you would rather not store the password: log into
myfitnesspal.com and the server reads the session from your browser. The cost is that MFP
sessions expire, so you will occasionally need to log in again.

Files this server writes to `~/.mfp_mcp/` (directory mode `0700`):

| File | Contents | Mode |
|------|----------|------|
| `cookies.json` | Session cookies — full account access, treat as a password | `0600` |

Note that this server can **modify your diary** and your custom foods.

## Usage Examples

Once configured, you can interact with your MyFitnessPal data through Claude:

### Food Diary
```
"Show me what I ate today"
"Get my food diary for 2026-01-05"
"What meals did I log yesterday?"
```

### Logging and Correcting Food
```
"Log a grilled chicken breast, 6 oz, for lunch"
"Add 2 cups of oatmeal to breakfast"
```

### Search Foods
```
"Search MyFitnessPal for chicken breast"
"Find nutrition info for Greek yogurt"
"Look up calories in a banana"
```

### Recipes
```
"Log a serving of my liver and onions for dinner"
"What recipes do I have?"
```

### Check Goals vs Actual
```
"Compare my nutrition goals to what I actually ate today"
"Am I on track with my protein intake?"
"How many calories do I have left today?"
```

## Key Management CLI

`scripts/store-key.ts` is a one-time setup tool that generates and stores `MFP_SECRET_KEY` in your OS keychain (macOS Keychain, Windows Credential Vault, Linux Secret Service). Node.js 18+ is required.

### Prerequisites

```bash
npm install
```

### Commands

| Command | What it does |
|---------|-------------|
| `npm run store-key` | Generate a new Fernet key and store it in the keychain |
| `npm run store-key -- --key <val>` | Store an existing key instead of generating one |
| `npm run store-key -- --overwrite` | Replace a key that is already stored |
| `npm run store-key -- --show` | Print the currently stored key |
| `npm run store-key -- --delete` | Remove the stored key from the keychain |

### Example output

```
✅ MFP_SECRET_KEY stored in OS keychain
   service : mfp-mcp
   account : MFP_SECRET_KEY
   source  : generated

Your key (use this to encrypt MFP_USERNAME / MFP_PASSWORD):

  abc123XYZ...==

Next — encrypt your credentials with Python:

  from cryptography.fernet import Fernet
  f = Fernet(b"abc123XYZ...==")
  print("MFP_USERNAME:", f.encrypt(b"your_email@example.com").decode())
  print("MFP_PASSWORD:", f.encrypt(b"your_password").decode())
```

## Project Structure

```
myfitnesspal-mcp-python/
├── Dockerfile              # Container deployment
├── package.json            # Node tooling (store-key CLI)
├── tsconfig.json           # TypeScript config for scripts/
├── pyproject.toml          # Python package configuration
├── README.md               # This file
├── scripts/
│   ├── export_cookies.py   # Chrome MFP session -> MFP_COOKIES_JSON
│   ├── refresh_local_cookies.py  # Browser session -> ~/.mfp_mcp/cookies.json
│   ├── smoke_test.py       # Call every tool over HTTP (local, Docker, Railway)
│   └── store-key.ts        # One-time key management CLI
├── src/
│   └── mfp_mcp/
│       ├── __init__.py     # Package initialization
│       ├── icon.png        # MyFitnessPal icon (serverInfo + /favicon.ico)
│       └── server.py       # MCP server implementation
└── tests/                  # Unit tests (no live MFP session needed)
```

## Development

### Setup Development Environment

```bash
# Clone and enter directory
git clone https://github.com/YOUR_USERNAME/myfitnesspal-mcp-python.git
cd myfitnesspal-mcp-python

# Create virtual environment (Python 3.10+ required)
python3 -m venv venv
source venv/bin/activate

# Upgrade pip and install with dev dependencies
pip install --upgrade pip
pip install -e ".[dev]"
```

### Run Tests

```bash
pytest
```

### Code Formatting

```bash
black src/
isort src/
ruff check src/
```

### Type Checking

```bash
mypy src/
```

## Docker Deployment

The image runs stdio by default. Set `MFP_MCP_TRANSPORT=streamable-http` to serve over HTTP; see the next section for the variables that mode needs.

```bash
docker build -t mfp-mcp .

# stdio, with Chrome cookies mounted (Linux example)
docker run -it --rm \
  -v ~/.config/google-chrome:/root/.config/google-chrome:ro \
  mfp-mcp

# HTTP
docker run --rm -p 8000:8000 \
  -e MFP_MCP_TRANSPORT=streamable-http \
  -e MFP_MCP_PATH_SECRET="$(openssl rand -hex 32)" \
  -e MFP_COOKIES_JSON="$(uv run python scripts/export_cookies.py)" \
  mfp-mcp
```

## Remote Deployment (Railway, for claude.ai and mobile)

claude.ai can only reach MCP servers on the public internet, so using this from the web or the mobile apps means hosting it. This fork runs it on Railway as a single always-on service built from the `Dockerfile`.

### How it is served

With `MFP_MCP_TRANSPORT=streamable-http`, the server listens on `0.0.0.0:$PORT` and serves MCP's streamable HTTP transport at `/<MFP_MCP_PATH_SECRET>/mcp`. It is stateless (no session affinity needed, restarts are harmless). Every other path returns 404, except `/favicon.ico`, which serves the MyFitnessPal icon.

| Variable              | Value                                                                   |
|-----------------------|-------------------------------------------------------------------------|
| `MFP_MCP_TRANSPORT`   | `streamable-http`                                                       |
| `MFP_MCP_PATH_SECRET` | alphanumeric, at least 32 characters (`openssl rand -hex 32`)           |
| `MFP_COOKIES_JSON`    | output of `scripts/export_cookies.py`                                   |
| `TZ`                  | your timezone, e.g. `America/New_York`; "today" defaults use it         |
| `MFP_STATE_DIR`       | `/data`, the mount path of a Railway volume; refreshed cookies go here  |
| `RAILWAY_RUN_UID`     | `0`; volumes mount root-owned, and the image otherwise runs as `mcp`    |
| `PORT`                | set by Railway                                                          |

Without `TZ` the container runs on UTC, and calls that default to today land on tomorrow's diary in the evening (after 8 pm in US Eastern).

### Security model

claude.ai custom connectors support OAuth or no authentication; they cannot send a fixed password or header. Running an OAuth server for one user is more machinery than the risk warrants, so the endpoint is unauthenticated and the access control is the secret in the URL path.

- **What the URL grants:** read and write access to your MFP food diary and custom foods, and read access to your recipes. It does not expose your MFP password, which the server never has. Treat the full URL as a password.
- **Where the secret lives:** the Railway variable, your claude.ai connector settings, and the URL itself. Railway terminates HTTPS, so the path is encrypted in transit. The server redacts the secret from its access log (`POST /<secret>/mcp`), so it does not appear in Railway's logs.
- **Guessing:** 32+ alphanumeric characters (256 bits from `openssl rand -hex 32`). Wrong paths get a plain 404.
- **Rotating:** set a new `MFP_MCP_PATH_SECRET` (Railway redeploys) and update the connector URL in claude.ai. The old URL stops working immediately.
- **The MFP session:** `MFP_COOKIES_JSON` holds live MFP session cookies, equivalent to being logged in. Anyone with access to the Railway project can read them.

### Deploying

```bash
railway login
railway init                      # create the project
railway add --service mfp-mcp     # create an empty service and link it
railway variables --set MFP_MCP_TRANSPORT=streamable-http \
                  --set "MFP_MCP_PATH_SECRET=$(openssl rand -hex 32)" \
                  --set TZ=America/New_York
# Log into myfitnesspal.com in a Chromium browser first
railway variables --set "MFP_COOKIES_JSON=$(uv run python scripts/export_cookies.py)"
railway volume add --mount-path /data
railway variables --set MFP_STATE_DIR=/data --set RAILWAY_RUN_UID=0
railway service source connect --repo <owner>/myfitnesspal-mcp-python \
                --branch main --service mfp-mcp   # deploy on every push to main
railway domain                    # assign a public *.up.railway.app domain
```

`export_cookies.py` reads the Chrome profile named `Default` (shown as "Personal" in a default setup). The first run triggers a macOS Keychain prompt for "Chrome Safe Storage"; if it times out, the script reports no session, so rerun it after allowing access.

Then in claude.ai: Settings → Connectors → Add custom connector, URL `https://<domain>/<secret>/mcp`, no OAuth. Connectors added on the web are available in the mobile apps. To print the URL later, from the linked repo:

```bash
railway variables --json | python3 -c "import json,sys; v=json.load(sys.stdin); print(f\"https://{v['RAILWAY_PUBLIC_DOMAIN']}/{v['MFP_MCP_PATH_SECRET']}/mcp\")"
```

Every push to `main` builds the `Dockerfile` and deploys; there is no manual step. `railway up --ci` still deploys the local working tree directly, e.g. to try uncommitted changes, and the next push replaces it. After a deploy, confirm it with `scripts/smoke_test.py --railway`.

### Smoke testing

`scripts/smoke_test.py` calls every tool through the MCP HTTP transport, the same path claude.ai uses, and prints a pass/fail line per tool. Run it before and after deploying:

```bash
uv run python scripts/smoke_test.py --local     # this checkout, run on your machine
uv run python scripts/smoke_test.py --docker    # the Docker image, configured as on Railway
uv run python scripts/smoke_test.py --railway   # the deployed service
```

Add `--writes` to also add and remove a diary entry and a recipe, and create and delete a custom food, all on 2025-01-01 or with throwaway names.

Local and Docker runs use your home IP. Some MFP endpoints behave differently from datacenter IPs (the HTML food search returned empty results from Railway, which is why search now uses the v2 API), so only `--railway` proves the deployed service works.

### Session refresh

MFP's session cookie (`__Secure-next-auth.session-token`) is a NextAuth JWT that expires 30 days after it was issued. Each `GET /api/auth/session` returns a new one with a fresh 30 days, which is how a browser stays logged in. In HTTP mode the server does the same: a background thread calls it at startup and every 6 hours and saves the refreshed cookies to `$MFP_STATE_DIR/cookies.json`. With that directory on a Railway volume, the session survives restarts and redeploys and never lapses as long as the service runs.

`MFP_COOKIES_JSON` only seeds the session. The saved file records a fingerprint of the variable it descends from, so setting a new export replaces the saved cookies; otherwise the saved (newer) ones win.

Re-export only if the refresh fails. It logs `MFP session refresh failed` to Railway's logs, and tools return authentication errors. Possible causes, none observed so far: MFP revoking sessions (e.g. on a password change) or enforcing a maximum session age. To re-export, log into MFP in Chrome and rerun the `MFP_COOKIES_JSON` line; Railway redeploys on variable changes.

## Troubleshooting

### "python: command not found" or wrong Python version

**Problem**: Python is not in PATH or you need to specify version.

**Solutions**:
1. On macOS/Linux, use `python3` instead of `python`
2. Check your version: `python3 --version` (must be 3.10+)
3. If needed, install Python 3.12 via Homebrew: `brew install python@3.12`
4. Then create venv with: `python3.12 -m venv venv`

### "pip install -e ." fails with "setup.py not found"

**Problem**: Your pip version is too old to support pyproject.toml builds.

**Solution**: Upgrade pip first:
```bash
pip install --upgrade pip
pip install -e .
```

### "Failed to authenticate with MyFitnessPal"

**Problem**: The server can't authenticate with your credentials or read browser cookies.

**Solutions**:
1. **Easiest (macOS)**: Log into [myfitnesspal.com](https://www.myfitnesspal.com)
   in any Chromium-based browser (Arc, Chrome, Edge, Brave, ...). The MCP
   will auto-discover the session on the next call.
2. **Force a refresh**: Run `scripts/refresh_local_cookies.py` — it scans
   every browser by default, or pass `--browser` (`arc`, `chrome`, `edge`,
   `brave`, `vivaldi`, `opera`, `firefox`).
3. **If using credentials**: Double-check your MFP_USERNAME and MFP_PASSWORD
   in the config. Note that the legacy form-login flow no longer works
   against MFP's NextAuth backend — credentials are only useful while
   `~/.mfp_mcp/cookies.json` still holds a valid session.
4. Try logging out and back in to MyFitnessPal in your browser.
5. Clear `~/.mfp_mcp/cookies.json` and let the auto-discovery rebuild it.
6. On **macOS**, the auto-discovery path reads each browser's `Safe Storage`
   password from your login keychain. On the very first run, macOS shows a
   dialog: *"<binary> wants to use information stored in your keychain"* —
   click **Always Allow**. If Claude Desktop is spawning the MCP headlessly
   in the background, this dialog can be easy to miss; if auto-discovery
   returns "no browser had a session", bring Claude Desktop to the
   foreground and retry so the prompt is visible. Once approved, the key
   is cached and the prompt won't repeat.

### "No module named 'mfp_mcp'"

**Problem**: Package not installed or wrong Python environment.

**Solutions**:
1. Ensure you're using the correct Python from your virtual environment
2. Reinstall the package: `pip install -e .`
3. Verify the path in your Claude Desktop config points to the venv Python:
   ```
   /path/to/project/venv/bin/python  # macOS/Linux
   C:\path\to\project\venv\Scripts\python.exe  # Windows
   ```

### Tools not appearing in Claude Desktop

**Problem**: MCP server not connecting.

**Solutions**:
1. Check the config file syntax (must be valid JSON - use a JSON validator)
2. Use **absolute paths** in the configuration (no `~` or relative paths)
3. Restart Claude Desktop completely (Cmd+Q on macOS, then relaunch)
4. Check Claude Desktop logs:
   - macOS: `~/Library/Logs/Claude/`
   - Windows: `%APPDATA%\Claude\logs\`

### Empty responses or no data

**Problem**: Authentication works but no data returned.

**Solutions**:
1. Verify you have data logged in MyFitnessPal for the requested date
2. Check the date format (YYYY-MM-DD)
3. Try a recent date where you know you have entries

### Double parentheses in terminal prompt like "((venv) )"

**Problem**: VS Code/Cursor Python extension bug with venv prompt.

**Solutions**:
1. Update the Python extension in VS Code/Cursor
2. Or manually fix the venv activate script - change line ~70 in `venv/bin/activate`:
   ```bash
   # Change from:
   PS1="("'(venv) '") ${PS1:-}"
   # To:
   PS1="(venv) ${PS1:-}"
   ```

## API Reference

### mfp_get_diary
Get the food diary for a date: entries by meal (each with `entry_id`, usable with `mfp_remove_food_from_diary`, plus name, brand, amount, servings and nutrition), per-meal and daily totals, `daily_goals`, and `remaining` (goal minus total).
- `date` (optional): YYYY-MM-DD format, defaults to today
- `response_format`: "markdown" or "json"

### mfp_search_food
Search for foods. Your recipes and custom foods whose names contain every query word come first (`source`: `"recipe"`, `"custom"`), then MyFitnessPal database results (`"database"`). A recipe's `mfp_id` is null if it cannot be logged yet (see `mfp_list_recipes`).
- `query` (required): Search term
- `limit` (optional): Max results (default 10, max 50)
- `response_format`: "markdown" or "json"

### mfp_get_food_details
Get detailed nutrition for a food item.
- `mfp_id` (required): MyFitnessPal food ID from search results
- `response_format`: "markdown" or "json"

### mfp_add_food_to_diary
Add a food item to your diary for a specific meal and date. Returns the new entry's `entry_id`, and fails if the entry does not appear in the diary afterwards.
- `mfp_id` (required): MyFitnessPal food ID from search results (use `mfp_search_food` first)
- `meal` (optional): Meal name - "Breakfast", "Lunch", "Dinner", or "Snacks" (default: "Breakfast")
- `date` (optional): YYYY-MM-DD format (default: today)
- `quantity` (optional): Number of servings (default: 1.0)
- `unit` (optional): Unit/serving size description (e.g., "1 cup", "100g")

**Example workflow:**
1. Use `mfp_search_food` to find a food item and get its `mfp_id`
2. Use `mfp_add_food_to_diary` with the `mfp_id` to add it to your diary

### mfp_create_custom_food
Create a private custom food in your account. Returns the new food's `id`, which
`mfp_add_food_to_diary` accepts.
- `description` (required): Food name as it appears in MFP
- `calories` (required): Calories per serving
- `brand_name` (optional): Brand; packaged = label brand, restaurant = venue, homemade = "Generic" (default: "Generic")
- `serving_amount` (optional): Serving size number (default: 100)
- `serving_unit` (optional): Serving unit, e.g. "g", "ml", "piece" (default: "g")
- Nutrients (all optional): `carbs`, `fiber`, `sugar`, `protein`, `fat`, `saturated_fat`,
  `polyunsaturated_fat`, `monounsaturated_fat`, `trans_fat`, `cholesterol` (mg), `sodium` (mg),
  `potassium` (mg), `vitamin_a`, `vitamin_c`, `calcium`, `iron` (last four are %DV)
- `country_code` (optional): Label convention (default: "NL") — **see the carbs note below**
- `public` (optional): Share publicly (default: false)
- `response_format`: "markdown" or "json"

**`carbs` is NET carbs, and `country_code` is what makes it so.** The field selects which
label convention your number follows, so it changes the meaning of `carbs`:

| `country_code` | `carbs` is read as | MFP stores |
|------|------|-------------|
| `"NL"` and other EU codes (labels exclude fibre) | **NET** | `net_carbs = carbs`, `carbohydrates = carbs + fiber` |
| omitted / US (labels include fibre) | TOTAL | `carbohydrates = carbs`, `net_carbs = carbs − fiber` |

Sending `carbs=42, fiber=8` stores `50/42` under `"NL"` but `42/34` without it. Pass the number
straight off the label and leave `country_code` matching that label's origin — do not pre-subtract
fibre.

**MyFitnessPal has no custom-food update endpoint.** To correct a food, create the corrected
version and then `mfp_delete_custom_food` the old one.

### mfp_list_recipes
List your saved recipes, for browsing: `recipe_id`, `name`, `servings`, nutrition `per_serving` (calories, protein, carbs, fat, fiber), and `mfp_id`, the food id that logs it with `mfp_add_food_to_diary`. `mfp_id` is null for a recipe not logged in the MFP app since it was last edited; log it there once to make it loggable here.
- `search` (optional): Words to match in the recipe name
- `response_format`: "markdown" or "json" (default "json")

### mfp_list_own_foods
List your own custom foods, newest first. Private custom foods do not reliably appear in
`mfp_search_food`, so this is how to find the `id` of something you created earlier.
- `search` (optional): Substring filter on the food name
- `limit` (optional): Max foods to return (default: 25)
- `response_format`: "markdown" or "json"

### mfp_delete_custom_food
Delete one of your custom foods. Destructive and not recoverable; MyFitnessPal may refuse if the
food is referenced by a logged diary entry.
- `food_id` (required): Food id from `mfp_create_custom_food` or `mfp_list_own_foods`

### mfp_remove_food_from_diary
Remove logged entries from your diary, including ones logged in the MFP app or website. Fails if a removed entry is still in the diary afterwards. Pass either:
- `entry_id`: Entry UUID from `mfp_get_diary` or `mfp_add_food_to_diary`, or
- `name_contains`: Case-insensitive substring of the food name, with optional `meal` filter and `max_matches` cap (default 1)
- `date` (optional): YYYY-MM-DD (default today)

## Security & Privacy

- **Encrypted Credentials**: Credentials can be stored as Fernet-encrypted ciphertext in your config. `MFP_SECRET_KEY` is resolved at runtime from the environment variable first, then the OS keychain (`mfp-mcp` / `MFP_SECRET_KEY`). See [Encrypted Credentials](#encrypted-credentials-enhanced-security) for setup.
- **OS Keychain**: Storing `MFP_SECRET_KEY` in the native keychain (macOS Keychain, Windows Credential Vault, Linux Secret Service) means the decryption key never touches the config file or any backup.
- **Plain Credentials**: If `MFP_SECRET_KEY` is absent from both environment and keychain, `MFP_USERNAME` and `MFP_PASSWORD` are used as-is (backward compatible).
- **Session Cookies**: After successful authentication, session cookies are cached in `~/.mfp_mcp/cookies.json` (restricted permissions) for 30 days.
- **Browser Cookies**: As a fallback, the server can read your browser cookies to authenticate with MyFitnessPal.
- **Remote Session Cookies**: In remote mode, `MFP_COOKIES_JSON` holds your MFP session in the host's environment (Railway variables). See [Security model](#security-model).
- **Local mode**: Over stdio, the server runs on your machine and your MyFitnessPal data travels only between your computer and myfitnesspal.com.
- **Remote mode**: Over HTTP, your data travels between myfitnesspal.com, the host (Railway), and Claude. The endpoint has no authentication beyond the secret URL path.

## License

MIT License - See [LICENSE](LICENSE) file for details.

## Acknowledgments

- [python-myfitnesspal](https://github.com/coddingtonbear/python-myfitnesspal) - Session setup for MyFitnessPal access
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk) - Model Context Protocol framework
- [Anthropic](https://anthropic.com) - Claude and the MCP specification
