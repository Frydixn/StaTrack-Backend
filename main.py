from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from slowapi import Limiter
from slowapi.util import get_remote_address
from slowapi.middleware import SlowAPIMiddleware
import httpx
import os
from dotenv import load_dotenv

load_dotenv()

# ── Configuración ──────────────────────────────────────────────
HENRIK_API_KEY       = os.getenv("HENRIK_API_KEY", "")
FRONTEND_URL         = os.getenv("FRONTEND_URL", "http://localhost:5173")
HENRIK_BASE          = "https://api.henrikdev.xyz"
HENRIK_HEADERS       = {"Authorization": HENRIK_API_KEY} if HENRIK_API_KEY else {}
SUPABASE_URL         = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
VALID_REGIONS        = {"eu", "na", "ap", "kr", "latam", "br"}

# Headers para llamadas directas a Supabase REST API
# Usando service_role bypasea RLS completamente
SUPABASE_HEADERS = {
    "apikey":        SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type":  "application/json",
    "Prefer":        "return=representation",
}

# ── Helpers de Supabase via HTTP directo ───────────────────────
async def sb_get(table: str, params: dict = {}) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(url, headers=SUPABASE_HEADERS, params=params)
        if r.status_code not in (200, 206):
            raise HTTPException(500, f"Supabase error {r.status_code}: {r.text[:200]}")
        return r.json()

async def sb_upsert(table: str, body, on_conflict: str = "") -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    headers = {**SUPABASE_HEADERS}
    if on_conflict:
        headers["Prefer"] = "return=representation,resolution=merge-duplicates"
    params = {}
    if on_conflict:
        params["on_conflict"] = on_conflict
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(url, headers=headers, json=body, params=params)
        if r.status_code not in (200, 201):
            raise HTTPException(500, f"Supabase error {r.status_code}: {r.text[:200]}")
        return r.json()

async def sb_insert(table: str, body) -> list:
    url = f"{SUPABASE_URL}/rest/v1/{table}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.post(url, headers=SUPABASE_HEADERS, json=body)
        if r.status_code not in (200, 201):
            raise HTTPException(500, f"Supabase error {r.status_code}: {r.text[:200]}")
        return r.json()

# ── Rate limiting ──────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=["60/minute"])

# ── App ────────────────────────────────────────────────────────
app = FastAPI(title="ValoQuest API Proxy", docs_url=None, redoc_url=None)
app.state.limiter = limiter
app.add_middleware(SlowAPIMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://valoquest.onrender.com",
        FRONTEND_URL,
        "http://localhost:5173",
        "http://localhost:4173",
    ],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

@app.options("/{rest_of_path:path}")
async def preflight(request: Request, rest_of_path: str = ""):
    origin = request.headers.get("origin", "")
    resp = Response(status_code=200)
    resp.headers["Access-Control-Allow-Origin"]  = origin
    resp.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    resp.headers["Access-Control-Allow-Headers"] = "*"
    resp.headers["Access-Control-Max-Age"]       = "600"
    return resp

# ── Helper HenrikDev ───────────────────────────────────────────
async def henrik_get(endpoint: str, params: dict = {}):
    async with httpx.AsyncClient(timeout=15.0) as client:
        r = await client.get(
            f"{HENRIK_BASE}{endpoint}",
            headers=HENRIK_HEADERS,
            params=params,
        )
        if r.status_code == 404:
            raise HTTPException(404, "Jugador no encontrado.")
        if r.status_code == 429:
            raise HTTPException(429, "Límite de API alcanzado.")
        if r.status_code != 200:
            raise HTTPException(r.status_code, f"Error HenrikDev: {r.text[:200]}")
        return r.json()

def validate_region(region: str):
    if region.lower() not in VALID_REGIONS:
        raise HTTPException(400, f"Región inválida: {region}")

# ── Endpoints HenrikDev ────────────────────────────────────────
@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "henrik_configured":   bool(HENRIK_API_KEY),
        "supabase_configured": bool(SUPABASE_URL and SUPABASE_SERVICE_KEY),
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
        return {"data": []}

@app.get("/api/matches/{region}/{name}/{tag}")
@limiter.limit("10/minute")
async def get_matches(
    request: Request, region: str, name: str, tag: str,
    size: int = 20, page: int = 1, mode: str = "competitive",
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

# ── Endpoints Supabase (HTTP directo, bypasea RLS con service_role) ──
@app.get("/api/db/player/{name}/{tag}")
@limiter.limit("20/minute")
async def get_player(request: Request, name: str, tag: str):
    rows = await sb_get("players", {
        "name": f"ilike.{name}",
        "tag":  f"ilike.{tag}",
        "limit": "1",
    })
    return rows[0] if rows else {}

@app.post("/api/db/player")
@limiter.limit("20/minute")
async def upsert_player(request: Request):
    body = await request.json()
    allowed = {"puuid","name","tag","region","account_level","last_updated"}
    filtered = {k: v for k, v in body.items() if k in allowed}
    result = await sb_upsert("players", filtered, on_conflict="puuid")
    return result

@app.get("/api/db/matches/{puuid}")
@limiter.limit("20/minute")
async def get_matches_db(request: Request, puuid: str):
    rows = await sb_get("player_matches", {
        "puuid":  f"eq.{puuid}",
        "select": "match_id,match_data",
    })
    return rows

@app.post("/api/db/matches")
@limiter.limit("20/minute")
async def upsert_matches(request: Request):
    body = await request.json()
    if not body:
        return []
    
    # Limitar a 20 partidas por request para no saturar RAM
    body = body[:20]
    
    # Reducir match_data solo a los campos necesarios
    def slim_match(row):
        md = row.get("match_data", {})
        meta = md.get("metadata", {})
        players = md.get("players", {})
        return {
            "puuid": row["puuid"],
            "match_id": row["match_id"],
            "match_data": {
                "metadata": {
                    "map": meta.get("map"),
                    "mode": meta.get("mode"),
                    "matchid": meta.get("matchid"),
                    "game_start": meta.get("game_start"),
                    "season_id": meta.get("season_id"),
                    "rounds_played": meta.get("rounds_played"),
                },
                "players": players,
                "teams": md.get("teams", {}),
                "kills": md.get("kills", []),
                "rounds": md.get("rounds", []),
            }
        }
    
    slimmed = [slim_match(r) for r in body]
    
    # Procesar en lotes de 5
    results = []
    for i in range(0, len(slimmed), 5):
        batch = slimmed[i:i+5]
        try:
            result = await sb_upsert("player_matches", batch, on_conflict="puuid,match_id")
            if isinstance(result, list):
                results.extend(result)
        except Exception as e:
            print(f"Error en lote {i}: {str(e)}")
            continue
            
    return results


@app.get("/api/db/stats/{puuid}")
@limiter.limit("20/minute")
async def get_stats(request: Request, puuid: str):
    rows = await sb_get("player_stats_snapshots", {
        "puuid":  f"eq.{puuid}",
        "select": "stats,created_at",
        "order":  "created_at.desc",
        "limit":  "1",
    })
    return rows[0] if rows else {}

@app.post("/api/db/stats")
@limiter.limit("20/minute")
async def upsert_stats(request: Request):
    body = await request.json()
    result = await sb_insert("player_stats_snapshots", body)
    return result

@app.get("/api/db/achievements/{puuid}")
@limiter.limit("20/minute")
async def get_achievements(request: Request, puuid: str):
    rows = await sb_get("player_achievements", {
        "puuid":  f"eq.{puuid}",
        "select": "achievement_id,unlocked_at",
    })
    return rows

@app.post("/api/db/achievements")
@limiter.limit("20/minute")
async def upsert_achievements(request: Request):
    body = await request.json()
    result = await sb_upsert("player_achievements", body, on_conflict="puuid,achievement_id")
    return result

@app.get("/api/db/pros")
@limiter.limit("20/minute")
async def get_pros(request: Request):
    rows = await sb_get("pro_players", {
        "active": "eq.true",
        "order":  "display_name.asc",
    })
    return rows

@app.get("/api/db/players/suggest")
@limiter.limit("30/minute")
async def suggest_players(request: Request, q: str = ""):
    if not q:
        return []
    rows = await sb_get("players", {
        "name": f"ilike.%{q}%",
        "limit": "10",
    })
    return rows