import os
import asyncio
import httpx
from dotenv import load_dotenv

load_dotenv()

SUPABASE_URL = os.getenv("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")

SUPABASE_HEADERS = {
    "apikey": SUPABASE_SERVICE_KEY,
    "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
    "Content-Type": "application/json",
    "Prefer": "return=representation",
}

def slim_match_data(md):
    if not md:
        return md
    meta = md.get("metadata", {})
    players = md.get("players", {})
    return {
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

async def main():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        print("Missing SUPABASE_URL or SUPABASE_SERVICE_KEY in .env file.")
        return
        
    async with httpx.AsyncClient(timeout=60.0) as client:
        print("Fetching matches from Supabase...")
        limit = 50
        total_updated = 0
        
        while True:
            url = f"{SUPABASE_URL}/rest/v1/player_matches"
            # Solamente traer partidas que no hayan sido procesadas aún (que tienen el objeto 'season' original)
            params = {
                "select": "puuid,match_id,match_data",
                "limit": limit,
                "match_data->metadata->season": "not.is.null"
            }
            r = await client.get(url, headers=SUPABASE_HEADERS, params=params)
            if r.status_code != 200:
                print(f"Error fetching matches: {r.status_code} - {r.text}")
                break
                
            rows = r.json()
            if not rows:
                print("No more rows to process.")
                break
                
            print(f"Processing batch of {len(rows)}...")
            
            slimmed_rows = []
            for row in rows:
                md = row.get("match_data")
                slimmed_md = slim_match_data(md)
                slimmed_rows.append({
                    "puuid": row["puuid"],
                    "match_id": row["match_id"],
                    "match_data": slimmed_md
                })
            
            for i in range(0, len(slimmed_rows), 10):
                batch = slimmed_rows[i:i+10]
                upsert_headers = {
                    **SUPABASE_HEADERS,
                    "Prefer": "resolution=merge-duplicates"
                }
                ur = await client.post(
                    f"{SUPABASE_URL}/rest/v1/player_matches",
                    headers=upsert_headers,
                    json=batch,
                    params={"on_conflict": "puuid,match_id"}
                )
                if ur.status_code not in (200, 201):
                    print(f"Error upserting batch: {ur.status_code} - {ur.text}")
                else:
                    total_updated += len(batch)
            
            print(f"Successfully processed {len(rows)} rows.")
            
        print(f"\nDone! Total updated rows: {total_updated}")

if __name__ == "__main__":
    asyncio.run(main())
