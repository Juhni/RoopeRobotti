#!/usr/bin/env python3
from __future__ import annotations
import argparse, asyncio, json, os, sys, time
from dataclasses import dataclass
from typing import Optional

import aiohttp
from dotenv import load_dotenv

AUTH_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"
AMC_BASE = "https://api.amc.husqvarna.dev"

load_dotenv()

@dataclass
class Config:
    client_id: str
    client_secret: str
    app_key: str
    refresh_token: str

def read_cfg() -> Config:
    def g(k: str) -> str:
        v = os.environ.get(k, "").strip()
        if not v:
            print(f"Missing {k} in .env", file=sys.stderr)
        return v
    return Config(
        client_id=g("HUSQ_CLIENT_ID"),
        client_secret=g("HUSQ_CLIENT_SECRET"),
        app_key=g("HUSQ_APP_KEY"),
        refresh_token=g("HUSQ_REFRESH_TOKEN"),
    )

async def get_access_token(cfg: Config) -> tuple[str, Optional[str], int]:
    """Return (access_token, rotated_refresh_token_or_None, expires_in_sec)."""
    data = {
        "grant_type": "refresh_token",
        "refresh_token": cfg.refresh_token,
        "client_id": cfg.client_id,
        "client_secret": cfg.client_secret,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(AUTH_URL, data=data, headers={"Content-Type": "application/x-www-form-urlencoded"}) as r:
            txt = await r.text()
            if r.status >= 400:
                print(f"Token refresh failed {r.status}: {txt}", file=sys.stderr)
                r.raise_for_status()
            js = await r.json()
            return js["access_token"], js.get("refresh_token"), int(js.get("expires_in", 300))

def _persist_rotated_rt(new_rt: str, env_path: str = ".env") -> None:
    try:
        import re
        text = ""
        if os.path.exists(env_path):
            text = open(env_path, "r", encoding="utf-8").read()
        if "HUSQ_REFRESH_TOKEN=" in text:
            text = re.sub(r"^HUSQ_REFRESH_TOKEN=.*$", f"HUSQ_REFRESH_TOKEN={new_rt}", text, flags=re.MULTILINE)
        else:
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"HUSQ_REFRESH_TOKEN={new_rt}\n"
        open(env_path, "w", encoding="utf-8").write(text)
        print("Note: refresh token rotated and saved to .env")
    except Exception as e:
        print(f"Warning: couldn't persist rotated token: {e}", file=sys.stderr)

async def list_mowers(access_token: str, app_key: str) -> dict:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Api-Key": app_key,
        "Authorization-Provider": "husqvarna",
        "Accept": "application/vnd.api+json",
    }
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{AMC_BASE}/v1/mowers") as r:
            txt = await r.text()
            if r.status >= 400:
                print(f"GET /v1/mowers {r.status}: {txt}", file=sys.stderr)
                r.raise_for_status()
            return await r.json()

def pick_mower(mowers: dict, mower_id: Optional[str], mower_name: Optional[str]) -> str:
    items = mowers.get("data") or []
    if not items:
        raise SystemExit("No mowers visible on this account.")
    if mower_id:
        for it in items:
            if it.get("id") == mower_id:
                return mower_id
        raise SystemExit(f"Mower id '{mower_id}' not found.")
    if mower_name:
        for it in items:
            name = ((it.get("attributes") or {}).get("system") or {}).get("name")
            if name == mower_name:
                return it["id"]
        raise SystemExit(f"Mower name '{mower_name}' not found.")
    if len(items) == 1:
        return items[0]["id"]
    print("Multiple mowers found; use --mower-id or --mower-name:", file=sys.stderr)
    for it in items:
        name = ((it.get("attributes") or {}).get("system") or {}).get("name")
        print(f"- {name or '(no-name)'} ({it.get('id')})", file=sys.stderr)
    raise SystemExit(2)

async def post_action(access_token: str, app_key: str, mower_id: str, payload: dict) -> None:
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Api-Key": app_key,
        "Authorization-Provider": "husqvarna",
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/vnd.api+json",
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{AMC_BASE}/v1/mowers/{mower_id}/actions",
                          headers=headers, data=json.dumps(payload)) as r:
            body = await r.text()
            if r.status >= 400:
                print(f"POST /v1/mowers/{mower_id}/actions {r.status}: {body}", file=sys.stderr)
                r.raise_for_status()

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Husqvarna Automower: pause/resume/park via REST")
    p.add_argument("--mower-id", help="Mower UUID")
    p.add_argument("--mower-name", help="Mower name (as in the app)")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("pause", help="Pause mowing now")

    rs = sub.add_parser("resume", help="Resume schedule")
    rs.add_argument("--fallback-start", action="store_true",
                    help="If ResumeSchedule fails, try Start")

    pk = sub.add_parser("park", help="Park the mower")
    pk.add_argument("--duration", type=int, help="Minutes to park; omit for until-further-notice")

    return p.parse_args()

async def main():
    args = parse_args()
    cfg = read_cfg()
    missing = [k for k,v in vars(cfg).items() if not v]
    if missing:
        raise SystemExit("Missing env vars in .env: " + ", ".join(missing))

    access, rotated, _expires = await get_access_token(cfg)
    if rotated and rotated != cfg.refresh_token:
        _persist_rotated_rt(rotated)

    mowers = await list_mowers(access, cfg.app_key)
    mid = pick_mower(mowers, args.mower_id, args.mower_name)

    if args.cmd == "pause":
        payload = {"data": {"type": "Pause"}}
        await post_action(access, cfg.app_key, mid, payload)
        print("OK: mower paused")

    elif args.cmd == "resume":
        try:
            await post_action(access, cfg.app_key, mid, {"data": {"type": "ResumeSchedule"}})
        except Exception:
            if args.fallback_start:
                await post_action(access, cfg.app_key, mid, {"data": {"type": "Start"}})
            else:
                raise
        print("OK: mower resumed")

    elif args.cmd == "park":
        if args.duration is None:
            payload = {"data": {"type": "ParkUntilFurtherNotice"}}
        else:
            payload = {"data": {"type": "Park", "attributes": {"duration": int(args.duration)}}}
        await post_action(access, cfg.app_key, mid, payload)
        if args.duration is None:
            print("OK: mower parked (until further notice)")
        else:
            print(f"OK: mower parked for {args.duration} minutes")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
