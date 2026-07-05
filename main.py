from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from supabase import create_client, Client
import httpx
import os

# ── Configuración ──────────────────────────────────────────────
HENRIK_API_KEY = os.getenv("HENRIK_API_KEY", "")
FRONTEND_URL   = os.getenv("FRONTEND_URL", "http://localhost:5173")
HENRIK_BASE    = "https://api.henrikdev.xyz"
HENRIK_HEADERS = {"Authorization": HENRIK_API_KEY} if HENRIK_API_KEY else {}
SUPABASE_URL         = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")


supabase_client: Client = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY) \
    if SUPABASE_URL and SUPABASE_SERVICE_KEY else None


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

# ── Endpoints de Supabase ──────────────────────────────────────

@app.get("/api/db/player/{name}/{tag}")
@limiter.limit("20/minute")
async def get_player(request: Request, name: str, tag: str):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    result = supabase_client.table("players") \
        .select("*") \
        .ilike("name", name) \
        .ilike("tag", tag) \
        .maybe_single() \
        .execute()
    return result.data or {}

@app.post("/api/db/player")
@limiter.limit("20/minute")
async def upsert_player(request: Request):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    body = await request.json()
    result = supabase_client.table("players") \
        .upsert(body) \
        .execute()
    return result.data

@app.get("/api/db/matches/{puuid}")
@limiter.limit("20/minute")
async def get_matches_db(request: Request, puuid: str):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    result = supabase_client.table("player_matches") \
        .select("match_id, match_data") \
        .eq("puuid", puuid) \
        .execute()
    return result.data or []

@app.post("/api/db/matches")
@limiter.limit("20/minute")
async def upsert_matches(request: Request):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    body = await request.json()  # lista de { puuid, match_id, match_data }
    result = supabase_client.table("player_matches") \
        .upsert(body, on_conflict="puuid,match_id") \
        .execute()
    return result.data

@app.get("/api/db/stats/{puuid}")
@limiter.limit("20/minute")
async def get_stats(request: Request, puuid: str):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    result = supabase_client.table("player_stats_snapshots") \
        .select("stats, created_at") \
        .eq("puuid", puuid) \
        .order("created_at", desc=True) \
        .limit(1) \
        .maybe_single() \
        .execute()
    return result.data or {}

@app.post("/api/db/stats")
@limiter.limit("20/minute")
async def upsert_stats(request: Request):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    body = await request.json()
    result = supabase_client.table("player_stats_snapshots") \
        .insert(body) \
        .execute()
    return result.data

@app.get("/api/db/achievements/{puuid}")
@limiter.limit("20/minute")
async def get_achievements(request: Request, puuid: str):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    result = supabase_client.table("player_achievements") \
        .select("achievement_id, unlocked_at") \
        .eq("puuid", puuid) \
        .execute()
    return result.data or []

@app.post("/api/db/achievements")
@limiter.limit("20/minute")
async def upsert_achievements(request: Request):
    if not supabase_client:
        raise HTTPException(500, "Supabase no configurado")
    body = await request.json()  # lista de { puuid, achievement_id }
    result = supabase_client.table("player_achievements") \
        .upsert(body, on_conflict="puuid,achievement_id") \
        .execute()
    return result.data