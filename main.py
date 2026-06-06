"""
Strava Dashboard Backend
========================
Handles OAuth token exchange server-side (no browser CORS issues) and
proxies activity data to the dashboard frontend.

Run locally:
    pip install fastapi uvicorn httpx
    python main.py
    → http://localhost:8000

Then in your browser, hit:
    http://localhost:8000/connect
...to start the OAuth flow. After authorizing, you're done — the backend
stores your tokens and auto-refreshes them. The dashboard then reads from
http://localhost:8000/api/*
"""
import json
import os
import time
from datetime import datetime, timedelta

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import RedirectResponse, HTMLResponse

# ─── CONFIG ──────────────────────────────────────────────────────────────────
CLIENT_ID = os.getenv("STRAVA_CLIENT_ID", "165986")
CLIENT_SECRET = os.getenv("STRAVA_CLIENT_SECRET", "f4b6255868a3b920b27a11f430ead6817a915f25")
REDIRECT_URI = os.getenv("REDIRECT_URI", "http://localhost:8000/callback")
TOKEN_FILE = "strava_tokens.json"

app = FastAPI(title="Strava Dashboard Backend")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # dashboard can be served from anywhere
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── TOKEN STORAGE ───────────────────────────────────────────────────────────
def load_tokens() -> dict:
    if os.path.exists(TOKEN_FILE):
        with open(TOKEN_FILE) as f:
            return json.load(f)
    return {}


def save_tokens(t: dict):
    with open(TOKEN_FILE, "w") as f:
        json.dump(t, f, indent=2)


async def get_valid_access_token() -> str:
    """Return a valid access token, refreshing it if expired."""
    tokens = load_tokens()
    if not tokens:
        raise HTTPException(401, "Not connected. Visit /connect first.")

    # Refresh if expiring within 5 minutes
    if tokens.get("expires_at", 0) < time.time() + 300:
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                "https://www.strava.com/oauth/token",
                data={
                    "client_id": CLIENT_ID,
                    "client_secret": CLIENT_SECRET,
                    "grant_type": "refresh_token",
                    "refresh_token": tokens["refresh_token"],
                },
            )
        resp.raise_for_status()
        new = resp.json()
        tokens.update({
            "access_token": new["access_token"],
            "refresh_token": new["refresh_token"],
            "expires_at": new["expires_at"],
        })
        save_tokens(tokens)

    return tokens["access_token"]


# ─── OAUTH FLOW ──────────────────────────────────────────────────────────────
@app.get("/connect")
def connect():
    """Step 1: Redirect user to Strava's authorization page."""
    url = (
        "https://www.strava.com/oauth/authorize"
        f"?client_id={CLIENT_ID}"
        "&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
        "&approval_prompt=force"
        "&scope=activity:read_all"
    )
    return RedirectResponse(url)


@app.get("/callback")
async def callback(code: str = None, scope: str = "", error: str = None):
    """Step 2: Strava redirects here with a code. Exchange it server-side."""
    if error:
        raise HTTPException(400, f"Authorization denied: {error}")
    if not code:
        raise HTTPException(400, "No code provided")
    if "activity:read_all" not in scope:
        return HTMLResponse(
            "<h2>⚠ Wrong scope</h2><p>Please reconnect and ensure you grant "
            "activity access. <a href='/connect'>Try again</a></p>"
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://www.strava.com/oauth/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "code": code,
                "grant_type": "authorization_code",
            },
        )
    resp.raise_for_status()
    data = resp.json()
    save_tokens({
        "access_token": data["access_token"],
        "refresh_token": data["refresh_token"],
        "expires_at": data["expires_at"],
        "athlete": data.get("athlete", {}),
    })
    return HTMLResponse(
        "<h1 style='font-family:sans-serif'>✅ Connected to Strava!</h1>"
        "<p style='font-family:sans-serif'>You can close this tab and return "
        "to your dashboard. It will now load your real data.</p>"
    )


@app.get("/status")
def status():
    tokens = load_tokens()
    if tokens.get("access_token"):
        ath = tokens.get("athlete", {})
        return {"connected": True, "athlete": ath}
    return {"connected": False}


# ─── DATA API (consumed by the dashboard) ────────────────────────────────────
@app.get("/api/athlete")
async def athlete():
    token = await get_valid_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.strava.com/api/v3/athlete",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


@app.get("/api/activities")
async def activities(weeks_ago: int = 0):
    """Return activities for a given week (0 = current week, 1 = last week...)."""
    token = await get_valid_access_token()

    now = datetime.now()
    monday = now - timedelta(days=now.weekday() + weeks_ago * 7)
    monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    sunday = monday + timedelta(days=6, hours=23, minutes=59, seconds=59)

    async with httpx.AsyncClient() as client:
        resp = await client.get(
            "https://www.strava.com/api/v3/athlete/activities",
            headers={"Authorization": f"Bearer {token}"},
            params={
                "after": int(monday.timestamp()),
                "before": int(sunday.timestamp()),
                "per_page": 100,
            },
        )
    resp.raise_for_status()
    return resp.json()


@app.get("/api/activity/{activity_id}")
async def activity_detail(activity_id: int):
    """Full detail for one activity (laps, splits, HR zones)."""
    token = await get_valid_access_token()
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"https://www.strava.com/api/v3/activities/{activity_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
    resp.raise_for_status()
    return resp.json()


@app.get("/")
def root():
    return HTMLResponse(
        "<div style='font-family:sans-serif;max-width:500px;margin:60px auto'>"
        "<h1>🏃 Strava Dashboard Backend</h1>"
        "<p>Status: running</p>"
        "<a href='/connect' style='display:inline-block;background:#fc4c02;"
        "color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;"
        "font-weight:bold'>Connect Strava →</a>"
        "</div>"
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
