"""Call every tool through the MCP streamable HTTP transport and check the results.

Targets:
    --local     run the server from this checkout in HTTP mode (auth from Chrome
                or MFP_COOKIES_JSON), test it, stop it
    --docker    build the Dockerfile and run the image the same way Railway does
                (auth: MFP_COOKIES_JSON exported from Chrome), test it, stop it
    --railway   the deployed service; URL built from `railway variables`
    --url URL   any running instance

Read-only by default. --writes also round-trips the write tools on a sacrificial
date (2025-01-01) and a throwaway custom food, deleting everything it creates.
Not exercised: mfp_set_goals, mfp_set_measurement, mfp_set_water (no undo) and
refresh_browser_cookies (needs a local browser).

Local and Docker runs use your home IP, so they cannot catch failures that only
happen from a datacenter IP; run against --railway after deploying.

    uv run python scripts/smoke_test.py --local
    uv run python scripts/smoke_test.py --railway --writes
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

REPO = Path(__file__).resolve().parents[1]
SCRATCH_DATE = "2025-01-01"
EXPECTED_TOOLS = {
    "mfp_get_diary", "mfp_search_food", "mfp_get_food_details", "mfp_add_food_to_diary",
    "mfp_remove_food_from_diary", "mfp_create_custom_food", "mfp_list_own_foods",
    "mfp_delete_custom_food", "mfp_get_measurements", "mfp_set_measurement",
    "mfp_get_exercises", "mfp_get_goals", "mfp_set_goals", "mfp_get_water", "mfp_set_water",
    "mfp_get_steps", "mfp_log_fast", "mfp_update_fast", "mfp_delete_fast", "mfp_get_report",
    "refresh_browser_cookies",
}


class Failed(Exception):
    pass


async def call(session, tool, **params):
    result = await session.call_tool(tool, {"params": params})
    text = "".join(getattr(c, "text", "") for c in result.content)
    if result.isError or text.startswith("Error"):
        raise Failed(text[:300])
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def check(cond, msg):
    if not cond:
        raise Failed(msg)


async def run_checks(url, writes):
    results = []

    async def step(name, coro):
        t0 = time.monotonic()
        try:
            out = await coro
            results.append((name, "ok", f"{time.monotonic() - t0:.1f}s"))
            return out
        except Exception as e:
            results.append((name, "FAIL", str(e)[:200]))
            return None

    async with streamablehttp_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def tools_listed():
                names = {t.name for t in (await session.list_tools()).tools}
                check(names == EXPECTED_TOOLS, f"tool set differs: {names ^ EXPECTED_TOOLS}")

            async def search():
                out = await call(session, "mfp_search_food", query="banana", limit=5,
                                 response_format="json")
                check(out["count"] > 0, "no results for 'banana'")
                return out["results"][0]["mfp_id"]

            async def diary():
                out = await call(session, "mfp_get_diary", response_format="json")
                check("meals" in out, "no meals key")

            await step("tools/list", tools_listed())
            mfp_id = await step("mfp_search_food", search())
            if mfp_id:
                await step("mfp_get_food_details",
                           call(session, "mfp_get_food_details", mfp_id=str(mfp_id)))
            await step("mfp_get_diary", diary())
            for tool in ("mfp_get_measurements", "mfp_get_exercises", "mfp_get_goals",
                         "mfp_get_water", "mfp_get_steps", "mfp_get_report",
                         "mfp_list_own_foods"):
                await step(tool, call(session, tool))

            if writes and mfp_id:
                async def diary_round_trip():
                    added = await call(session, "mfp_add_food_to_diary", mfp_id=str(mfp_id),
                                       meal="Snacks", date=SCRATCH_DATE)
                    check(added.get("verified"), f"add not verified: {added}")
                    day = await call(session, "mfp_get_diary", date=SCRATCH_DATE,
                                     response_format="json")
                    ids = [e.get("entry_id") for m in day["meals"].values() for e in m["entries"]]
                    check(added["entry_id"] in ids, "added entry has no entry_id in diary")
                    removed = await call(session, "mfp_remove_food_from_diary",
                                         date=SCRATCH_DATE, entry_id=added["entry_id"])
                    check(removed.get("verified"), f"remove not verified: {removed}")

                async def custom_food_round_trip():
                    name = f"mfp-mcp smoke test {uuid.uuid4().hex[:8]}"
                    await call(session, "mfp_create_custom_food", description=name,
                               calories=1, response_format="json")
                    own = await call(session, "mfp_list_own_foods", search=name,
                                     response_format="json")
                    foods = own.get("foods") if isinstance(own, dict) else own
                    check(foods, f"created food not listed: {own}")
                    await call(session, "mfp_delete_custom_food", food_id=str(foods[0]["id"]))

                async def fast_round_trip():
                    fast_id = str(uuid.uuid4()).upper()
                    await call(session, "mfp_log_fast", id=fast_id,
                               fast_started=f"{SCRATCH_DATE}T01:00:00Z",
                               fast_ended=f"{SCRATCH_DATE}T02:00:00Z")
                    await call(session, "mfp_update_fast", id=fast_id,
                               fast_started=f"{SCRATCH_DATE}T01:00:00Z",
                               fast_ended=f"{SCRATCH_DATE}T03:00:00Z")
                    await call(session, "mfp_delete_fast", id=fast_id)

                await step("diary add/get/remove", diary_round_trip())
                await step("custom food create/list/delete", custom_food_round_trip())
                await step("fast log/update/delete", fast_round_trip())

    width = max(len(r[0]) for r in results)
    for name, status, detail in results:
        print(f"{name:<{width}}  {status:<4}  {detail}")
    return all(r[1] == "ok" for r in results)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_for(port, proc_alive, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not proc_alive():
            raise SystemExit("server exited before it started listening")
        try:
            socket.create_connection(("127.0.0.1", port), timeout=1).close()
            return
        except OSError:
            time.sleep(0.5)
    raise SystemExit(f"server did not listen on port {port} within {timeout}s")


def exported_cookies():
    sys.path.insert(0, str(REPO / "src"))
    from mfp_mcp.server import try_chromium_browsers_for_session_cookies

    result = try_chromium_browsers_for_session_cookies()
    if not result:
        raise SystemExit("No Chromium browser has a logged-in MyFitnessPal session.")
    return json.dumps(result[1])


def railway_url():
    out = subprocess.run(["railway", "variables", "--json"], cwd=REPO, check=True,
                         capture_output=True, text=True).stdout
    v = json.loads(out)
    return f"https://{v['RAILWAY_PUBLIC_DOMAIN']}/{v['MFP_MCP_PATH_SECRET']}/mcp"


def main():
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its request log prints the secret
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--local", action="store_true")
    target.add_argument("--docker", action="store_true")
    target.add_argument("--railway", action="store_true")
    target.add_argument("--url")
    parser.add_argument("--writes", action="store_true")
    args = parser.parse_args()

    secret = secrets.token_hex(32)
    port = free_port()
    stop = None
    if args.local:
        env = {**os.environ, "MFP_MCP_TRANSPORT": "streamable-http",
               "MFP_MCP_PATH_SECRET": secret, "PORT": str(port)}
        proc = subprocess.Popen([sys.executable, "-m", "mfp_mcp.server"], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        wait_for(port, lambda: proc.poll() is None)
        stop = proc.terminate
        url = f"http://127.0.0.1:{port}/{secret}/mcp"
    elif args.docker:
        tag = "mfp-mcp-smoke"
        subprocess.run(["docker", "build", "-q", "-t", tag, "."], cwd=REPO, check=True,
                       stdout=subprocess.DEVNULL)
        cid = subprocess.run(
            ["docker", "run", "-d", "--rm", "-p", f"127.0.0.1:{port}:8000",
             "-e", "MFP_MCP_TRANSPORT=streamable-http", "-e", f"MFP_MCP_PATH_SECRET={secret}",
             "-e", "PORT=8000", "-e", f"TZ={os.environ.get('TZ', 'America/New_York')}",
             "-e", f"MFP_COOKIES_JSON={exported_cookies()}", tag],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        stop = lambda: subprocess.run(["docker", "stop", cid], capture_output=True)  # noqa: E731
        wait_for(port, lambda: True)
        time.sleep(2)  # docker's port proxy accepts before uvicorn is up
        url = f"http://127.0.0.1:{port}/{secret}/mcp"
    elif args.railway:
        url = railway_url()
    else:
        url = args.url

    try:
        ok = asyncio.run(run_checks(url, args.writes))
    finally:
        if stop:
            stop()
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
