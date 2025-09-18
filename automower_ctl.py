#!/usr/bin/env python3
from __future__ import annotations
import argparse
import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import aiohttp
from dotenv import load_dotenv

# OAuth + AMC endpoints
AUTH_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"
AMC_BASE = "https://api.amc.husqvarna.dev"

load_dotenv()

# ---------------- Config ----------------
@dataclass
class Config:
    client_id: str
    client_secret: str
    app_key: str
    refresh_token: str

def read_cfg() -> Config:
    def g(k: str) -> str:
        return os.environ.get(k, "").strip()
    cfg = Config(
        client_id=g("HUSQ_CLIENT_ID"),
        client_secret=g("HUSQ_CLIENT_SECRET"),
        app_key=g("HUSQ_APP_KEY"),
        refresh_token=g("HUSQ_REFRESH_TOKEN"),
    )
    missing = [k for k, v in {
        "HUSQ_CLIENT_ID": cfg.client_id,
        "HUSQ_CLIENT_SECRET": cfg.client_secret,
        "HUSQ_APP_KEY": cfg.app_key,
        "HUSQ_REFRESH_TOKEN": cfg.refresh_token
    }.items() if not v]
    if missing:
        raise SystemExit("Missing env vars in .env: " + ", ".join(missing))
    return cfg

# ---------------- Auth ----------------
async def get_access_token(cfg: Config) -> Tuple[str, Optional[str], int]:
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
                print(f"ERROR: token refresh failed {r.status}: {txt}", file=sys.stderr)
                r.raise_for_status()
            js = await r.json()
            return js["access_token"], js.get("refresh_token"), int(js.get("expires_in", 300))

def _persist_rotated_rt(new_rt: str, env_path: str = ".env") -> None:
    try:
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

# ---------------- API helpers ----------------
def _auth_headers(access_token: str, app_key: str) -> Dict[str, str]:
    return {
        "Authorization": f"Bearer {access_token}",
        "X-Api-Key": app_key,
        "Authorization-Provider": "husqvarna",
        "Accept": "application/vnd.api+json",
    }

async def get_mowers(access_token: str, app_key: str) -> Dict[str, Any]:
    async with aiohttp.ClientSession(headers=_auth_headers(access_token, app_key)) as s:
        async with s.get(f"{AMC_BASE}/v1/mowers") as r:
            txt = await r.text()
            if r.status >= 400:
                print(f"ERROR: GET /v1/mowers {r.status}: {txt}", file=sys.stderr)
                r.raise_for_status()
            return await r.json()

def pick_mower(mowers: Dict[str, Any], mower_id: Optional[str], mower_name: Optional[str]) -> Dict[str, Any]:
    items = mowers.get("data") or []
    if not items:
        raise SystemExit("No mowers visible on this account.")
    if mower_id:
        for it in items:
            if it.get("id") == mower_id:
                return it
        raise SystemExit(f"Mower id '{mower_id}' not found.")
    if mower_name:
        for it in items:
            name = ((it.get("attributes") or {}).get("system") or {}).get("name")
            if name == mower_name:
                return it
        raise SystemExit(f"Mower name '{mower_name}' not found.")
    if len(items) == 1:
        return items[0]
    print("Multiple mowers found; choose one with --mower-id or --mower-name:", file=sys.stderr)
    for it in items:
        name = ((it.get("attributes") or {}).get("system") or {}).get("name")
        print(f"- {name or '(no-name)'}  ({it.get('id')})", file=sys.stderr)
    raise SystemExit(2)

def find_work_area_id(mower: Dict[str, Any], name_or_id: str) -> int:
    attrs = mower.get("attributes") or {}
    areas = attrs.get("workAreas") or []
    # name match
    for wa in areas:
        if wa.get("name") == name_or_id:
            return int(wa.get("workAreaId"))
    # id match
    for wa in areas:
        if str(wa.get("workAreaId")) == str(name_or_id):
            return int(wa.get("workAreaId"))
    raise SystemExit(f"Work area '{name_or_id}' not found on this mower.")

async def post_action(access_token: str, app_key: str, mower_id: str, payload: Dict[str, Any]) -> None:
    headers = _auth_headers(access_token, app_key)
    headers["Content-Type"] = "application/vnd.api+json"
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{AMC_BASE}/v1/mowers/{mower_id}/actions",
                          headers=headers, data=json.dumps(payload)) as r:
            body = await r.text()
            if r.status >= 400:
                print(f"ERROR: POST /v1/mowers/{mower_id}/actions {r.status}: {body}", file=sys.stderr)
                r.raise_for_status()

# ---------------- Action wrappers ----------------
# Core motion
async def do_pause(tok: str, app_key: str, mower_id: str):
    await post_action(tok, app_key, mower_id, {"data": {"type": "Pause"}})
    print("OK: mower paused")

async def do_resume(tok: str, app_key: str, mower_id: str, fallback_start: bool):
    try:
        await post_action(tok, app_key, mower_id, {"data": {"type": "ResumeSchedule"}})
    except Exception:
        if fallback_start:
            await post_action(tok, app_key, mower_id, {"data": {"type": "Start"}})
        else:
            raise
    print("OK: mower resumed")

async def do_start(tok: str, app_key: str, mower_id: str,
                   work_area_id: Optional[int], duration: Optional[int]):
    attrs: Dict[str, Any] = {}
    if work_area_id is not None:
        attrs["workAreaId"] = int(work_area_id)
    if duration is not None:
        attrs["duration"] = int(duration)
    payload: Dict[str, Any] = {"data": {"type": "Start"}}
    if attrs:
        payload["data"]["attributes"] = attrs
    await post_action(tok, app_key, mower_id, payload)
    area_txt = f" in work area {work_area_id}" if work_area_id is not None else ""
    dur_txt = f" for {duration} min" if duration is not None else ""
    print(f"OK: mower started{area_txt}{dur_txt}")

# Parking
async def do_park(tok: str, app_key: str, mower_id: str,
                  duration: Optional[int], until_next: bool):
    if until_next:
        payload = {"data": {"type": "ParkUntilNextSchedule"}}
    elif duration is None:
        payload = {"data": {"type": "ParkUntilFurtherNotice"}}
    else:
        payload = {"data": {"type": "Park", "attributes": {"duration": int(duration)}}}
    await post_action(tok, app_key, mower_id, payload)
    if until_next:
        print("OK: mower parked until next schedule")
    elif duration is None:
        print("OK: mower parked (until further notice)")
    else:
        print(f"OK: mower parked for {duration} minutes")

# Errors
async def do_confirm_error(tok: str, app_key: str, mower_id: str):
    await post_action(tok, app_key, mower_id, {"data": {"type": "ConfirmError"}})
    print("OK: error confirmed (if any)")

# Settings
async def do_set_cutting_height(tok: str, app_key: str, mower_id: str, height: int):
    payload = {"data": {"type": "settings", "attributes": {"cuttingHeight": int(height)}}}
    await post_action(tok, app_key, mower_id, payload)
    print(f"OK: cutting height set to {height}")

async def do_set_headlight(tok: str, app_key: str, mower: Dict[str, Any], mode: str):
    caps = ((mower.get("attributes") or {}).get("capabilities") or {})
    if not caps.get("headlights", False):
        name = ((mower.get("attributes") or {}).get("system") or {}).get("name") or mower.get("id")
        print(f"Model has no headlights capability (headlights=False) for '{name}'.", file=sys.stderr)
        return
    mower_id = mower.get("id")
    payload = {"data": {"type": "settings", "attributes": {"headlight": {"mode": mode}}}}
    await post_action(tok, app_key, mower_id, payload)
    print(f"OK: headlight mode set to {mode}")

# Discovery / listing
def print_list_actions(mower: Dict[str, Any]) -> None:
    attrs = mower.get("attributes") or {}
    sysinfo = (attrs.get("system") or {})
    caps = (attrs.get("capabilities") or {})
    work_areas = (attrs.get("workAreas") or [])
    mower_name = sysinfo.get("name") or mower.get("id")
    model = sysinfo.get("model", "(unknown)")

    print(f"Supported actions for {mower_name} ({model}):")
    print("- Pause")
    print("- ResumeSchedule")
    print("- Start [--duration N] [--work-area NAME|ID]")
    print("- Park [--duration N]")
    print("- ParkUntilNextSchedule")
    print("- ParkUntilFurtherNotice")
    if caps.get("canConfirmError"):
        print("- ConfirmError")
    else:
        print("- ConfirmError (not supported by this model)")

    if caps.get("workAreas"):
        print("\nWork areas on this mower:")
        for wa in work_areas:
            print(f"  • {wa.get('name')} (id={wa.get('workAreaId')})")
        print("Note: many backends require --duration when you pass --work-area.")

    if caps.get("headlights"):
        print("\nSettings (headlights supported):")
        print("- set-headlight ALWAYS_ON|ALWAYS_OFF|EVENING_ONLY")
    else:
        print("\nSettings:")
        print("- set-headlight (not supported on this model)")
    print("- set-height <value>  (model-specific scale)")

    # Current state snapshot
    mower_state = (attrs.get("mower") or {})
    activity = mower_state.get("activity")
    state = mower_state.get("state")
    print(f"\nCurrent state: state={state}, activity={activity}")

# ---------------- CLI ----------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Husqvarna Automower CLI (REST)")
    p.add_argument("--mower-id", help="Mower UUID")
    p.add_argument("--mower-name", help="Mower name as shown in app")

    sub = p.add_subparsers(dest="cmd", required=True)

    # Discovery
    sub.add_parser("list-actions", help="Print supported actions for the selected mower")

    # Core
    sub.add_parser("pause", help="Pause mowing now")

    rs = sub.add_parser("resume", help="Resume schedule (fallback to Start if requested)")
    rs.add_argument("--fallback-start", action="store_true", help="If ResumeSchedule fails, try Start")

    st = sub.add_parser("start", help="Start mowing (optionally in a specific work area)")
    st.add_argument("--work-area", help="Work area NAME or ID to start in")
    st.add_argument("--duration", type=int,
                    help="Minutes to mow (often required when --work-area is given)")

    # Parking
    pk = sub.add_parser("park", help="Park mower")
    pk.add_argument("--duration", type=int, help="Minutes to park; omit for until-further-notice")
    pk.add_argument("--until-next", action="store_true", help="Park until next schedule window")

    # Errors
    sub.add_parser("confirm-error", help="Confirm/clear an error if confirmable")

    # Settings
    ch = sub.add_parser("set-height", help="Set cutting height")
    ch.add_argument("height", type=int, help="Cutting height value (model-specific scale)")

    hl = sub.add_parser("set-headlight", help="Set headlight mode (if supported)")
    hl.add_argument("mode", choices=["ALWAYS_ON", "ALWAYS_OFF", "EVENING_ONLY"], help="Headlight mode")

    return p

# ---------------- Main ----------------
async def main():
    parser = build_parser()
    args = parser.parse_args()

    cfg = read_cfg()
    access, rotated, _ = await get_access_token(cfg)
    if rotated and rotated != cfg.refresh_token:
        _persist_rotated_rt(rotated)

    mowers = await get_mowers(access, cfg.app_key)
    mower = pick_mower(mowers, args.mower_id, args.mower_name)
    mid = mower.get("id")

    try:
        if args.cmd == "list-actions":
            print_list_actions(mower)
        elif args.cmd == "pause":
            await do_pause(access, cfg.app_key, mid)
        elif args.cmd == "resume":
            await do_resume(access, cfg.app_key, mid, args.fallback_start)
        elif args.cmd == "start":
            wa_id = None
            if getattr(args, "work_area", None):
                wa_id = find_work_area_id(mower, args.work_area)
            await do_start(access, cfg.app_key, mid, wa_id, getattr(args, "duration", None))
        elif args.cmd == "park":
            await do_park(access, cfg.app_key, mid, args.duration, args.until_next)
        elif args.cmd == "confirm-error":
            await do_confirm_error(access, cfg.app_key, mid)
        elif args.cmd == "set-height":
            await do_set_cutting_height(access, cfg.app_key, mid, args.height)
        elif args.cmd == "set-headlight":
            await do_set_headlight(access, cfg.app_key, mower, args.mode)
        else:
            parser.print_help()
    except aiohttp.ClientResponseError as e:
        url = e.request_info.real_url if e.request_info else ""
        print(f"HTTP {e.status} on {url}", file=sys.stderr)
        try:
            body = await e.response.text()  # type: ignore[attr-defined]
            print(body, file=sys.stderr)
        except Exception:
            pass
        raise

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except SystemExit as e:
        sys.exit(e.code)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
