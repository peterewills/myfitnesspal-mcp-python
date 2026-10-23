"""Print the MyFitnessPal session cookies from a local Chromium browser as JSON.

The output is the value for MFP_COOKIES_JSON on a remote deployment:

    railway variables --set "MFP_COOKIES_JSON=$(uv run python scripts/export_cookies.py)"
"""

import json
import sys

from mfp_mcp.server import try_chromium_browsers_for_session_cookies

result = try_chromium_browsers_for_session_cookies()
if not result:
    sys.exit("No Chromium browser has a logged-in MyFitnessPal session.")
browser, cookies = result
print(f"Using MyFitnessPal session from {browser}", file=sys.stderr)
print(json.dumps(cookies))
