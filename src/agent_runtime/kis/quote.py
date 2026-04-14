"""Minimal KIS domestic current-price client.

Vendored from /home/ubuntu/alpha-engine/src/alpha_engine/kis/simpleki.py to keep
runtime free of alpha-engine imports. Token cache lives in the same YAML file
guarded by an fcntl flock so multiple processes (alpha-engine + runtime) can
share credentials safely.

Key file path: env ``KIS_CONFIG_PATH`` (default ``/home/ubuntu/key.yaml``).
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
import yaml


URL_BASE = "https://openapi.koreainvestment.com:9443"
DEFAULT_KEY_PATH = "/home/ubuntu/key.yaml"


def _key_path() -> str:
    return os.getenv("KIS_CONFIG_PATH", DEFAULT_KEY_PATH)


def _load_yaml_config(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _save_yaml_config(filepath: str, config: dict) -> None:
    target = Path(filepath)
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_stat = target.stat() if target.exists() else None
    fd, tmp_path = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            yaml.dump(config, f, default_flow_style=False, allow_unicode=True)
            f.flush()
            os.fsync(f.fileno())
        if existing_stat is not None:
            try:
                os.chown(tmp_path, existing_stat.st_uid, existing_stat.st_gid)
            except PermissionError:
                pass
            os.chmod(tmp_path, stat.S_IMODE(existing_stat.st_mode))
        os.replace(tmp_path, filepath)
    finally:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)


def _acquire_file_lock(filepath: str):
    lock_path = f"{filepath}.lock"
    lock_file = open(lock_path, "a+", encoding="utf-8")
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    return lock_file


def _gettoken(url_base: str, app_key: str, app_secret: str) -> str:
    headers = {"content-type": "application/json"}
    body = {
        "grant_type": "client_credentials",
        "appkey": app_key,
        "appsecret": app_secret,
    }
    r = requests.post(f"{url_base}/oauth2/tokenP", headers=headers, data=json.dumps(body), timeout=30)
    r.raise_for_status()
    return (r.json() or {}).get("access_token", "")


def _get_or_refresh_token(filepath: str) -> tuple[str, str, str]:
    """Return (access_token, app_key, app_secret), refreshing once per Seoul day."""
    today = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y%m%d")
    lock_file = _acquire_file_lock(filepath)
    try:
        cfg = _load_yaml_config(filepath)
        app_key = (cfg.get("APP_KEY") or "").strip()
        app_secret = (cfg.get("APP_SECRET") or "").strip()
        if not app_key or not app_secret:
            raise RuntimeError(f"KIS keyfile {filepath} missing APP_KEY/APP_SECRET")

        if cfg.get("ACCESS_TOKEN_DATE") == today and cfg.get("ACCESS_TOKEN"):
            return cfg["ACCESS_TOKEN"], app_key, app_secret

        token = _gettoken(URL_BASE, app_key, app_secret)
        cfg["ACCESS_TOKEN"] = token
        cfg["ACCESS_TOKEN_DATE"] = today
        _save_yaml_config(filepath, cfg)
        return token, app_key, app_secret
    finally:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        lock_file.close()


def get_current_price_domestic(symbol: str) -> dict[str, Any]:
    """Fetch raw KIS current-price `output` for a Korean ticker."""
    token, app_key, app_secret = _get_or_refresh_token(_key_path())
    path = "/uapi/domestic-stock/v1/quotations/inquire-price"
    headers = {
        "Content-Type": "application/json",
        "authorization": f"Bearer {token}",
        "appKey": app_key,
        "appSecret": app_secret,
        "custtype": "P",
        "tr_id": "FHKST01010100",
    }
    params = {
        "FID_COND_MRKT_DIV_CODE": "J",
        "FID_INPUT_ISCD": str(symbol).zfill(6),
    }
    r = requests.get(f"{URL_BASE}{path}", headers=headers, params=params, timeout=30)
    r.raise_for_status()
    j = r.json() or {}
    if j.get("rt_cd") != "0":
        raise RuntimeError(f"KIS domestic price failed: {j.get('msg_cd')} {j.get('msg1')}")
    return dict(j.get("output") or {})


def kis_quote(symbol: str) -> dict[str, Any]:
    """Return a normalized quote dict for a Korean ticker.

    Fields: price (int), prev_close (int), change (int), change_pct (float),
    name (str|None), raw (dict).
    """
    out = get_current_price_domestic(symbol)
    try:
        price = int(float(out.get("stck_prpr") or 0))
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"KIS quote missing stck_prpr for {symbol}: {out}") from exc
    if price <= 0:
        raise RuntimeError(f"KIS quote returned non-positive price for {symbol}: {out}")
    try:
        prev_close = int(float(out.get("stck_sdpr") or 0))
    except (TypeError, ValueError):
        prev_close = 0
    try:
        change = int(float(out.get("prdy_vrss") or 0))
    except (TypeError, ValueError):
        change = 0
    try:
        change_pct = float(out.get("prdy_ctrt") or 0.0)
    except (TypeError, ValueError):
        change_pct = 0.0
    return {
        "symbol": str(symbol).zfill(6),
        "price": price,
        "prev_close": prev_close,
        "change": change,
        "change_pct": change_pct,
        "name": out.get("hts_kor_isnm"),
        "raw": out,
    }
