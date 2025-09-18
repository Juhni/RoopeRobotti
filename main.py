#!/usr/bin/env python3
"""
Automower → console + (optional) InfluxDB v2 logger (REST-based)
- Loads .env (python-dotenv)
- Refreshes OAuth access token and persists rotated refresh tokens to .env
- Calls Automower Connect REST /v1/mowers via aiohttp
- Prints a readable line each poll; --debug and custom --trace

Usage:
  pip install aiohttp python-dotenv influxdb-client
  python main.py --once --debug
"""

from __future__ import annotations
import asyncio
import logging
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp
from dotenv import load_dotenv

# -------- InfluxDB (optional) --------
try:
    from influxdb_client import InfluxDBClient, Point
    from influxdb_client.client.write_api import SYNCHRONOUS
except Exception:  # pragma: no cover
    InfluxDBClient = None  # type: ignore
    Point = None  # type: ignore
    SYNCHRONOUS = None  # type: ignore

# -------- Logging with TRACE --------
TRACE_LEVEL_NUM = 5
logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")
def trace(self, message, *args, **kwargs):
    if self.isEnabledFor(TRACE_LEVEL_NUM):
        self._log(TRACE_LEVEL_NUM, message, args, **kwargs)
logging.Logger.trace = trace  # type: ignore[attr-defined]
log = logging.getLogger("automower")

def setup_logging(debug: bool, trace_flag: bool) -> None:
    level = logging.INFO
    if debug:
        level = logging.DEBUG
    if trace_flag:
        level = TRACE_LEVEL_NUM
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(logging.Formatter("%(asctime)s | %(levelname)-5s | %(name)s | %(message)s"))
    log.setLevel(level)
    log.addHandler(h)
    for noisy in ("aiohttp", "influxdb_client", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING if level > TRACE_LEVEL_NUM else level)

# -------- Config --------
load_dotenv()  # .env -> os.environ

@dataclass
class Config:
    client_id: str
    client_secret: str
    api_key: str        # X-Api-Key (Application key)
    refresh_token: str
    poll_seconds: int = 30
    influx_url: str = "http://127.0.0.1:8086"
    influx_token: str = ""       # leave empty to disable Influx writes
    influx_org: str = "home"
    influx_bucket: str = "automower"
    measurement: str = "automower_status"

def read_config() -> Config:
    return Config(
        client_id=os.environ.get("HUSQ_CLIENT_ID", "").strip(),
        client_secret=os.environ.get("HUSQ_CLIENT_SECRET", "").strip(),
        api_key=os.environ.get("HUSQ_APP_KEY", "").strip(),
        refresh_token=os.environ.get("HUSQ_REFRESH_TOKEN", "").strip(),
        poll_seconds=int(os.environ.get("POLL_SECONDS", "30") or "30"),
        influx_url=os.environ.get("INFLUX_URL", "http://127.0.0.1:8086").strip(),
        influx_token=os.environ.get("INFLUX_TOKEN", "").strip(),
        influx_org=os.environ.get("INFLUX_ORG", "home").strip(),
        influx_bucket=os.environ.get("INFLUX_BUCKET", "automower").strip(),
    )

# -------- Persist rotated refresh token --------
def _persist_refresh_token_to_env(new_rt: str, env_path: str = ".env") -> None:
    try:
        p = Path(env_path)
        if not p.exists():
            p.write_text(f"HUSQ_REFRESH_TOKEN={new_rt}\n", encoding="utf-8")
            return
        lines = p.read_text(encoding="utf-8").splitlines()
        for i, ln in enumerate(lines):
            if ln.strip().startswith("HUSQ_REFRESH_TOKEN="):
                lines[i] = f"HUSQ_REFRESH_TOKEN={new_rt}"
                break
        else:
            lines.append(f"HUSQ_REFRESH_TOKEN={new_rt}")
        p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        log.warning("Could not persist rotated refresh token to .env: %s", e)

# -------- OAuth (refresh -> access) --------
AUTH_TOKEN_URL = "https://api.authentication.husqvarnagroup.dev/v1/oauth2/token"
AMC_BASE = "https://api.amc.husqvarna.dev"

class StaticAuth:
    """Minimal token refresher using refresh_token (handles rotation)."""
    def __init__(self, client_id: str, client_secret: str, api_key: str, refresh_token: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_key = api_key
        self.refresh_token = refresh_token
        self._access_token: Optional[str] = None
        self._expires_at: float = 0.0

    async def async_get_access_token(self) -> str:
        now = time.time()
        if not self._access_token or now >= self._expires_at - 30:
            data = {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            async with aiohttp.ClientSession() as s:
                async with s.post(AUTH_TOKEN_URL, data=data, headers=headers) as r:
                    txt = await r.text()
                    if r.status >= 400:
                        log.error("Token refresh failed %s: %s", r.status, txt)
                        r.raise_for_status()
                    tok = await r.json()
                    self._access_token = tok["access_token"]
                    self._expires_at = now + int(tok.get("expires_in", 300))
                    log.debug("Refreshed access token; expires_in=%ss", int(tok.get("expires_in", 300)))

                    # Handle refresh-token rotation
                    new_rt = tok.get("refresh_token")
                    if new_rt and new_rt != self.refresh_token:
                        self.refresh_token = new_rt
                        _persist_refresh_token_to_env(new_rt)
                        log.debug("Stored rotated refresh token.")
        return self._access_token  # type: ignore[return-value]

# -------- Influx helper --------
class InfluxWriter:
    def __init__(self, cfg: Config):
        self.enabled = bool(cfg.influx_token and InfluxDBClient and Point)
        self.cfg = cfg
        if not self.enabled:
            log.info("InfluxDB disabled (no token or client not installed).")
            self.client = None
            self.write_api = None
        else:
            self.client = InfluxDBClient(url=cfg.influx_url, token=cfg.influx_token, org=cfg.influx_org)
            self.write_api = self.client.write_api(write_options=SYNCHRONOUS)

    def write(self, mower_id: str, name: str, fields: Dict[str, Any], ts: datetime):
        if not self.enabled:
            return
        p = Point(self.cfg.measurement).tag("mower_id", mower_id).tag("name", name).time(ts)
        for k, v in fields.items():
            if v is None:
                continue
            if isinstance(v, bool):
                p = p.boolean_field(k, v)
            elif isinstance(v, int):
                p = p.field(k, int(v))
            elif isinstance(v, float):
                p = p.field(k, float(v))
            else:
                p = p.field(k, str(v))
        self.write_api.write(bucket=self.cfg.influx_bucket, record=p)

# -------- Automower REST fetch --------
async def fetch_mowers(auth: StaticAuth, api_key: str) -> Dict[str, Any]:
    access_token = await auth.async_get_access_token()
    headers = {
        "Authorization": f"Bearer {access_token}",
        "X-Api-Key": api_key,
        "Authorization-Provider": "husqvarna",
    }
    async with aiohttp.ClientSession(headers=headers) as s:
        async with s.get(f"{AMC_BASE}/v1/mowers") as r:
            text = await r.text()
            log.trace("AMC /v1/mowers raw: %s", text)
            if r.status >= 400:
                log.error("AMC /v1/mowers failed %s: %s", r.status, text)
                r.raise_for_status()
            return await r.json()

def _enum_num(val: Optional[str]) -> Optional[int]:
    if val is None:
        return None
    table = {
        "UNKNOWN": 0, "PARKED": 1, "PAUSED": 2, "STOPPED": 3, "IN_OPERATION": 4,
        "MOWING": 5, "GOING_HOME": 6, "LEAVING": 7, "CHARGING": 8, "ERROR": 9,
        "SLEEPING": 10, "FATAL_ERROR": 11, "RESTRICTED": 12, "DISCONNECTED": 13,
        "MANUAL_START_REQUIRED": 14, "AUTO": 20, "MAIN_AREA": 21, "SECONDARY_AREA": 22, "HOME": 23,
    }
    return table.get(str(val).upper(), 0)

def _summary_line(name: str, attrs: Dict[str, Any]) -> str:
    batt = (attrs.get("battery") or {}).get("batteryPercent")
    mower = attrs.get("mower") or {}
    act = mower.get("activity")
    state = mower.get("state")
    mode = mower.get("mode")
    pos = (attrs.get("positions") or [{}])[-1] if attrs.get("positions") else {}
    lat, lng = pos.get("latitude"), pos.get("longitude")
    parts = [f"name={name}", f"battery={batt}%", f"state={state}", f"activity={act}", f"mode={mode}"]
    if lat is not None and lng is not None:
        parts.append(f"pos=({lat:.5f},{lng:.5f})")
    return " | ".join(parts)

# -------- Main polling --------
async def run_once(cfg: Config, influx: InfluxWriter, auth: StaticAuth) -> int:
    if not all([cfg.client_id, cfg.client_secret, cfg.api_key, cfg.refresh_token]):
        log.error("Missing required env vars (HUSQ_CLIENT_ID/SECRET/APP_KEY/REFRESH_TOKEN).")
        return 2

    payload = await fetch_mowers(auth, cfg.api_key)
    data = payload.get("data") or []

    if not data:
        log.warning("No mowers returned. Is this Husqvarna account linked to any mowers?")
        return 1

    now = datetime.now(timezone.utc)
    for item in data:
        mower_id = item.get("id")
        attrs = item.get("attributes") or {}
        name = (attrs.get("system") or {}).get("name") or mower_id

        battery = (attrs.get("battery") or {}).get("batteryPercent")
        mower = attrs.get("mower") or {}
        pos = (attrs.get("positions") or [{}])[-1] if attrs.get("positions") else {}
        cutting = attrs.get("cutting") or {}
        conn = (attrs.get("metadata") or {}).get("connected")

        activity = mower.get("activity")
        state = mower.get("state")
        mode = mower.get("mode")
        lat = pos.get("latitude")
        lng = pos.get("longitude")
        height = (attrs.get("settings") or {}).get("cuttingHeight") or cutting.get("height")

        log.info(_summary_line(name, attrs))
        log.debug("Raw mower item: %r", item)

        fields = {
            "battery_percent": float(battery) if battery is not None else None,
            "cutting_height": float(height) if height is not None else None,
            "connected": bool(conn) if conn is not None else None,
            "activity": activity,
            "state": state,
            "mode": mode,
            "activity_num": _enum_num(activity),
            "state_num": _enum_num(state),
            "mode_num": _enum_num(mode),
            "lat": float(lat) if lat is not None else None,
            "lng": float(lng) if lng is not None else None,
        }
        influx.write(mower_id or "unknown", name, fields, now)

    return 0

async def run_loop(cfg: Config, influx: InfluxWriter, auth: StaticAuth) -> int:
    while True:
        try:
            await run_once(cfg, influx, auth)
        except Exception as e:
            log.exception("Polling error: %s", e)
        await asyncio.sleep(max(1, cfg.poll_seconds))

# -------- CLI --------
def parse_args(argv: list[str]):
    import argparse
    p = argparse.ArgumentParser(description="Automower logger (REST)")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--debug", action="store_true", help="Enable DEBUG logging")
    g.add_argument("--trace", action="store_true", help="Enable ultra-verbose TRACE logging")
    p.add_argument("--once", action="store_true", help="Fetch one snapshot and exit")
    p.add_argument("--poll-seconds", type=int, default=None, help="Override POLL_SECONDS from env")
    return p.parse_args(argv)

def main(argv: list[str]) -> int:
    args = parse_args(argv)
    setup_logging(args.debug, args.trace)
    cfg = read_config()
    if args.poll_seconds is not None:
        cfg.poll_seconds = args.poll_seconds
    influx = InfluxWriter(cfg)

    # Create a single auth object and reuse it (keeps rotated refresh token in memory)
    auth = StaticAuth(cfg.client_id, cfg.client_secret, cfg.api_key, cfg.refresh_token)

    log.debug("Config: %r", cfg)
    try:
        if args.once:
            return asyncio.run(run_once(cfg, influx, auth))
        else:
            return asyncio.run(run_loop(cfg, influx, auth))
    except KeyboardInterrupt:
        log.info("Interrupted by user.")
        return 130

if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
