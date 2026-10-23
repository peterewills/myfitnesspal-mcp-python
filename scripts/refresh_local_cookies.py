"""Re-read the MyFitnessPal session from a local browser into ~/.mfp_mcp/cookies.json.

For local (stdio) use. The server already falls back to reading your browser
when the saved cookies stop working; this forces it, or picks a browser.

    uv run python scripts/refresh_local_cookies.py              # scan every browser
    uv run python scripts/refresh_local_cookies.py --browser arc

Remote deployments renew their session themselves; see the README's
"Session refresh".
"""

import argparse

from mfp_mcp.server import refresh_browser_cookies

parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
parser.add_argument(
    "--browser",
    default="auto",
    help="auto (default), arc, chrome, chromium, edge, brave, vivaldi, opera, or firefox",
)
print(refresh_browser_cookies(parser.parse_args().browser))
