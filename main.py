from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
import httpx
import os

# ── Configuración ──────────────────────────────────────────────
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY", "")
FRONTEND_URL   = os.getenv("FRONTEND_URL", "http://localhost:5173")
HENRIK_BASE    = "https://api.henrikdev.xyz"
HENRIK_HEADERS = {"Authorization": HENRIK_API_KEY} if HENRIK_API_KEY else {}

VALID_REGIONS = {"eu", "na", "ap", "kr", "latam", "br"}

# ── Rate limiting ──────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

# ── App ────────────────────────────────────────────────────────
app = FastAPI(title="ValoQuest API Proxy", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:4173",
    ],
    allow_methods=["GET"],
    allow_headers=["*"],
)

# ── Helper ─────────────────────────────────────────────────────
async def henrik_get(endpoint: str, params: dict = {}):
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{HENRIK_BASE}{endpoint}",
            headers=HENRIK_HEADERS,
            params=params,
        )
        if r.status_code == 404:
            raise HTTPException(404, "Jugador no encontrado. Revisá el Riot ID.")
        if r.status_code == 429:
            raise HTTPException(429, "Límite de la API alcanzado. Intentá en un momento.")
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"Error de HenrikDev: {r.text[:200]}")
        return r.json()

def validate_region(region: str):
    if region.lower() not in VALID_REGIONS:
        raise HTTPException(400, f"Región inválida. Válidas: {', '.join(VALID_REGIONS)}")

# ── Endpoints ──────────────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "henrik_configured": bool(HENRIK_API_KEY),
    }

@app.get("/api/account/{name}/{tag}")
@limiter.limit("10/minute")
async def get_account(request: Request, name: str, tag: str):
    return await henrik_get(f"/valorant/v1/account/{name}/{tag}")

@app.get("/api/mmr/{region}/{name}/{tag}")
@limiter.limit("10/minute")
async def get_mmr(request: Request, region: str, name: str, tag: str):
    validate_region(region)
    return await henrik_get(f"/valorant/v2/mmr/{region}/{name}/{tag}")

@app.get("/api/mmr-history/{region}/{name}/{tag}")
@limiter.limit("10/minute")
async def get_mmr_history(request: Request, region: str, name: str, tag: str):
    validate_region(region)
    try:
        return await henrik_get(f"/valorant/v1/mmr-history/{region}/{name}/{tag}")
    except HTTPException:
        return {"data": []}   # silencioso si falla

@app.get("/api/matches/{region}/{name}/{tag}")
@limiter.limit("10/minute")
async def get_matches(
    request: Request,
    region: str,
    name: str,
    tag: str,
    size: int = 20,
    page: int = 1,
    mode: str = "competitive",
):
    validate_region(region)
    size = max(1, min(size, 20))
    page = max(1, page)
    valid_modes = {"competitive","unrated","spikerush","deathmatch","escalation","teamdeathmatch"}
    if mode not in valid_modes:
        mode = "competitive"
    return await henrik_get(
        f"/valorant/v3/matches/{region}/{name}/{tag}",
        params={"size": size, "page": page, "mode": mode},
    )