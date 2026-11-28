import argparse
import html
import json
import os
import re
import sys
import traceback
from datetime import datetime, timezone
from pathlib import Path

import requests
import spotipy
from dotenv import load_dotenv
from spotipy.oauth2 import SpotifyOAuth
from ytmusicapi import YTMusic

load_dotenv()

SPOTIFY_CLIENT_ID = os.getenv("SPOTIFY_CLIENT_ID")
SPOTIFY_CLIENT_SECRET = os.getenv("SPOTIFY_CLIENT_SECRET")
SPOTIFY_REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI")
SPOTIFY_REFRESH_TOKEN = os.getenv("SPOTIFY_REFRESH_TOKEN")
YT_MUSIC_HEADER_AUTH_JSON = os.getenv("YT_MUSIC_HEADER_AUTH_JSON")

WEBHOOK_URL = os.getenv("SYNC_WEBHOOK_URL")
EXCLUDED_PLAYLISTS = os.getenv("EXCLUDED_PLAYLISTS", "")

SCOPE = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private"
)

STATE_FILE = Path("sync_state.json")
BATCH = 100

# Clients are created in init_clients(), inside main()'s error handling, so a
# login failure (expired token / cookies) gets reported to Discord instead of
# crashing before the report code can run.
sp = None
ytmusic = None
SP_USER_ID = None


def get_spotify():
    auth = SpotifyOAuth(
        client_id=SPOTIFY_CLIENT_ID,
        client_secret=SPOTIFY_CLIENT_SECRET,
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SCOPE,
    )
    if SPOTIFY_REFRESH_TOKEN:
        token_info = auth.refresh_access_token(SPOTIFY_REFRESH_TOKEN)
        return spotipy.Spotify(auth=token_info["access_token"])
    return spotipy.Spotify(auth_manager=auth)


def init_clients():
    global sp, ytmusic, SP_USER_ID
    sp = get_spotify()
    ytmusic = YTMusic(YT_MUSIC_HEADER_AUTH_JSON)
    SP_USER_ID = sp.current_user()["id"]


def load_state():
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {}
    state.setdefault("sp_to_yt", {})
    state.setdefault("yt_to_sp", {})
    state.setdefault("not_found_on_yt", [])
    state.setdefault("not_found_on_sp", [])
    state.setdefault("descriptions", {})
    return state


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def norm(s):
    """Lowercase and strip noise so 'Song (feat. X) - Remastered' ~ 'Song'."""
    s = (s or "").lower()
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\s-\s.*$", "", s)
    s = re.sub(r"\b(feat|ft)\b\.?.*$", "", s)
    s = re.sub(r"[^\w\s]", "", s)
    return re.sub(r"\s+", " ", s).strip()


def make_key(title, artist):
    return f"{norm(title)}|{norm(artist)}"

PLACEHOLDER_DESCRIPTIONS = {"imported from spotify", "synced from spotify", "synced from youtube music"}
SP_DESC_LIMIT = 300


def clean_desc(d):
    """Strip HTML (Spotify returns links/entities) and ignore our own placeholders."""
    d = re.sub(r"<[^>]+>", "", d or "")
    d = html.unescape(d).strip()
    return "" if d.lower() in PLACEHOLDER_DESCRIPTIONS else d


def desc_equal(a, b):
    """Compare descriptions, tolerating Spotify's length limit."""
    return (a or "")[:SP_DESC_LIMIT] == (b or "")[:SP_DESC_LIMIT]


def make_track(track_id, title, artist):
    return {"id": track_id, "title": title, "artist": artist, "key": make_key(title, artist)}


def dedupe(tracks):
    seen, out = set(), []
    for t in tracks:
        if t["key"] not in seen:
            seen.add(t["key"])
            out.append(t)
    return out


def sp_paginate(results):
    items = list(results["items"])
    while results.get("next"):
        results = sp.next(results)
        items.extend(results["items"])
    return items


def get_spotify_playlists(excluded=frozenset()):
    """name(lower) -> {'id', 'name', 'description'} for playlists owned by the current user."""
    playlists = sp_paginate(sp.current_user_playlists(limit=50))
    return {
        p["name"].lower(): {"id": p["id"], "name": p["name"], "description": p.get("description")}
        for p in playlists
        if p and p["owner"]["id"] == SP_USER_ID and p["name"].lower() not in excluded
    }


def get_spotify_tracks(playlist_id):
    tracks = []
    for item in sp_paginate(sp.playlist_items(playlist_id, limit=100)):
        t = item.get("track") or item.get("item")
        if not t or not t.get("id") or t.get("type", "track") != "track":
            continue
        artist = t["artists"][0]["name"] if t.get("artists") else ""
        tracks.append(make_track(t["id"], t["name"], artist))
    return dedupe(tracks)


def find_on_spotify(track):
    queries = [f"track:{track['title']} artist:{track['artist']}", f"{track['title']} {track['artist']}"]
    for q in queries:
        items = sp.search(q=q, type="track", limit=5)["tracks"]["items"]
        if not items:
            continue
        for it in items:
            artist = it["artists"][0]["name"] if it["artists"] else ""
            if make_key(it["name"], artist) == track["key"]:
                return it["id"]
        return items[0]["id"]
    return None


def create_spotify_playlist(name, description=""):
    pl = sp.user_playlist_create(SP_USER_ID, name, public=False, description=description[:SP_DESC_LIMIT])
    return pl["id"]


def add_to_spotify(playlist_id, track_ids):
    for i in range(0, len(track_ids), BATCH):
        sp.playlist_add_items(playlist_id, [f"spotify:track:{t}" for t in track_ids[i:i + BATCH]])


def get_ytmusic_playlists(excluded=frozenset()):
    """name(lower) -> {'id', 'name', 'tracks', 'description'} for playlists owned by the user."""
    result = {}
    try:
        library = ytmusic.get_library_playlists(limit=None) or []
    except Exception as e:
        raise RuntimeError(f"Could not fetch YouTube Music playlists: {e}")

    for p in library:
        pid = p["playlistId"]
        if pid in ("LM", "SE"):
            continue
        if p["title"].lower() in excluded:
            continue
        try:
            data = ytmusic.get_playlist(pid, limit=None)
        except Exception as e:
            print(f"  Warning: couldn't read YT playlist '{p['title']}': {e}")
            continue
        if not data.get("owned", False):
            continue

        tracks = []
        for t in data.get("tracks", []):
            if not t.get("videoId"):
                continue
            artist = t["artists"][0]["name"] if t.get("artists") else ""
            tracks.append(make_track(t["videoId"], t["title"], artist))
        result[p["title"].lower()] = {
            "id": pid, "name": p["title"], "tracks": dedupe(tracks),
            "description": data.get("description"),
        }
    return result


def find_on_youtube(track):
    results = ytmusic.search(f"{track['title']} {track['artist']}", filter="songs")
    if not results:
        return None
    for r in results[:5]:
        artist = r["artists"][0]["name"] if r.get("artists") else ""
        if r.get("videoId") and make_key(r["title"], artist) == track["key"]:
            return r["videoId"]
    return results[0].get("videoId")


def create_ytmusic_playlist(name, description=""):
    try:
        return ytmusic.create_playlist(name, description, privacy_status="PRIVATE")
    except Exception as e:
        print(f"  Error creating playlist (retrying with sanitized name): {e}")
        sanitized = re.sub(r"[<>]", "", name).strip() or "Imported Playlist"
        return ytmusic.create_playlist(sanitized, description, privacy_status="PRIVATE")


def add_to_ytmusic(playlist_id, video_ids):
    for i in range(0, len(video_ids), BATCH):
        ytmusic.add_playlist_items(playlist_id, video_ids[i:i + BATCH], duplicates=False)


def tracks_to_add(source, target, id_map, not_found):
    """Tracks in `source` that aren't already represented in `target`."""
    target_ids = {t["id"] for t in target}
    target_keys = {t["key"] for t in target}
    missing = []
    for t in source:
        mapped = id_map.get(t["id"])
        if mapped and mapped in target_ids:
            continue
        if t["key"] in target_keys or t["key"] in not_found:
            continue
        missing.append(t)
    return missing


def resolve(missing, finder, state, forward_map, backward_map, not_found_list, target_ids):
    """Search the other service for each missing track.

    Returns a dict {id on the other service: "Artist - Title"} for tracks that
    should actually be added. If the search lands on a track that is already in
    the target playlist (e.g. romanized vs. native names), the match is recorded
    but nothing is added.
    """
    found = {}
    for i, t in enumerate(missing, 1):
        print(f"    Searching {i}/{len(missing)}: {t['title']} - {t['artist']}")
        try:
            other_id = finder(t)
        except Exception as e:
            print(f"      Error searching: {e}")
            other_id = None
        if other_id:
            state[forward_map][t["id"]] = other_id
            state[backward_map][other_id] = t["id"]
            if other_id not in target_ids:
                found.setdefault(other_id, f"{t['artist']} - {t['title']}")
            else:
                print("      Already in target playlist under a different name, skipping")
        else:
            not_found_list.append(t["key"])
            print("      Not found")
    return found


def sync_playlist(name, sp_pl, yt_pl, state, dry_run):
    """Sync one playlist. Returns a list of labels for the songs that were added."""
    added = []

    if sp_pl is None:
        print("  Playlist only on YouTube Music -> creating on Spotify")
        desc = clean_desc(yt_pl.get("description"))
        sp_pl = {"id": None if dry_run else create_spotify_playlist(name, desc),
                 "name": name, "description": desc[:SP_DESC_LIMIT]}
        sp_tracks = []
    else:
        sp_tracks = get_spotify_tracks(sp_pl["id"])

    if yt_pl is None:
        print("  Playlist only on Spotify -> creating on YouTube Music")
        desc = clean_desc(sp_pl.get("description"))
        yt_pl = {"id": None if dry_run else create_ytmusic_playlist(name, desc),
                 "name": name, "tracks": [], "description": desc}
    yt_tracks = yt_pl["tracks"]

    print(f"  Spotify: {len(sp_tracks)} tracks | YouTube Music: {len(yt_tracks)} tracks")

    add_to_yt = tracks_to_add(sp_tracks, yt_tracks, state["sp_to_yt"], set(state["not_found_on_yt"]))
    add_to_sp = tracks_to_add(yt_tracks, sp_tracks, state["yt_to_sp"], set(state["not_found_on_sp"]))

    if add_to_yt:
        print(f"  {len(add_to_yt)} track(s) to add to YouTube Music")
        found = resolve(add_to_yt, find_on_youtube, state, "sp_to_yt", "yt_to_sp",
                        state["not_found_on_yt"], {t["id"] for t in yt_tracks})
        video_ids = list(found)
        if video_ids and not dry_run:
            add_to_ytmusic(yt_pl["id"], video_ids)
        added += [f"{label} (→ YT Music)" for label in found.values()]
        print(f"  {'Would add' if dry_run else 'Added'} {len(video_ids)} to YouTube Music")

    if add_to_sp:
        print(f"  {len(add_to_sp)} track(s) to add to Spotify")
        found = resolve(add_to_sp, find_on_spotify, state, "yt_to_sp", "sp_to_yt",
                        state["not_found_on_sp"], {t["id"] for t in sp_tracks})
        spotify_ids = list(found)
        if spotify_ids and not dry_run:
            add_to_spotify(sp_pl["id"], spotify_ids)
        added += [f"{label} (→ Spotify)" for label in found.values()]
        print(f"  {'Would add' if dry_run else 'Added'} {len(spotify_ids)} to Spotify")

    if not add_to_yt and not add_to_sp:
        print("  Tracks already in sync")

    sync_description(name, sp_pl, yt_pl, state, dry_run)
    return added


def sync_description(name, sp_pl, yt_pl, state, dry_run):
    """
    Keep playlist descriptions in sync.
    - One side empty      -> copy from the other side.
    - Both set, differ    -> whichever side changed since the last sync wins.
    - Both changed / no history -> Spotify wins.
    """
    key = name.lower()
    sp_d = clean_desc(sp_pl.get("description"))
    yt_d = clean_desc(yt_pl.get("description"))
    last = state["descriptions"].get(key)

    if desc_equal(sp_d, yt_d):
        state["descriptions"][key] = yt_d if len(yt_d) > len(sp_d) else sp_d
        return

    if not yt_d:
        direction = "sp_to_yt"
    elif not sp_d:
        direction = "yt_to_sp"
    elif last is not None and desc_equal(yt_d, last) and not desc_equal(sp_d, last):
        direction = "sp_to_yt"
    elif last is not None and desc_equal(sp_d, last) and not desc_equal(yt_d, last):
        direction = "yt_to_sp"
    else:
        print("  Description differs on both sides -> using Spotify's")
        direction = "sp_to_yt"

    if direction == "sp_to_yt":
        print(f"  {'Would update' if dry_run else 'Updating'} YouTube Music description")
        if not dry_run and yt_pl["id"]:
            ytmusic.edit_playlist(yt_pl["id"], description=sp_d)
        state["descriptions"][key] = sp_d
    else:
        print(f"  {'Would update' if dry_run else 'Updating'} Spotify description")
        if not dry_run and sp_pl["id"]:
            sp.playlist_change_details(sp_pl["id"], description=yt_d[:SP_DESC_LIMIT])
        state["descriptions"][key] = yt_d


def send_report(results, errors):
    """results: {"Playlist name": ["Artist - Title (→ Service)", ...]}"""
    if not WEBHOOK_URL:
        return
    results = results or {}
    errors = errors or []

    total = sum(len(songs) for songs in results.values())
    sections = []
    for playlist, songs in results.items():
        if not songs:
            continue
        shown = "\n".join(f"• {s}" for s in songs[:10])
        more = f"\n…and {len(songs) - 10} more" if len(songs) > 10 else ""
        sections.append(f"**{playlist}** (+{len(songs)})\n{shown}{more}")

    embed = {
        "title": f"🔄 Playlist sync: {total} new song{'s' if total != 1 else ''}",
        "description": ("\n\n".join(sections) or "Everything is up to date.")[:4000],
        "color": 0xE74C3C if errors else (0x2ECC71 if total else 0x95A5A6),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if errors:
        embed["fields"] = [{"name": "⚠️ Errors", "value": "\n".join(errors)[:1000]}]

    payload = {
        "username": "Playlist Sync",
        "embeds": [embed],
        "allowed_mentions": {"parse": []},
    }
    try:
        requests.post(WEBHOOK_URL, json=payload, timeout=10).raise_for_status()
    except requests.RequestException as e:
        print(f"Couldn't send Discord report: {e}")


def main():
    parser = argparse.ArgumentParser(description="Two-way Spotify <-> YouTube Music playlist sync")
    parser.add_argument("--dry-run", action="store_true", help="show changes without making them")
    parser.add_argument("--exclude", action="append", default=[], metavar="NAME",
                        help="playlist name to skip (repeatable); adds to EXCLUDED_PLAYLISTS in .env")
    args = parser.parse_args()

    excluded = {n.strip().lower() for n in EXCLUDED_PLAYLISTS.split(";") if n.strip()}
    excluded |= {n.strip().lower() for n in args.exclude if n.strip()}
    if excluded:
        print(f"Excluding {len(excluded)} playlist name(s): {', '.join(sorted(excluded))}")

    results, errors = {}, []
    state = load_state()

    try:
        init_clients()
        sp_playlists = get_spotify_playlists(excluded)
        yt_playlists = get_ytmusic_playlists(excluded)

        names = sorted(set(sp_playlists) | set(yt_playlists))
        print(f"Spotify: {len(sp_playlists)} playlists | YouTube Music: {len(yt_playlists)} playlists")
        print(f"{len(names)} unique playlist names to process{' (DRY RUN)' if args.dry_run else ''}\n")

        for key in names:
            sp_pl = sp_playlists.get(key)
            yt_pl = yt_playlists.get(key)
            display = (sp_pl or yt_pl)["name"]

            print(f"▶️  {display}")
            try:
                results[display] = sync_playlist(display, sp_pl, yt_pl, state, args.dry_run)
                print("✅ Done\n")
            except Exception as e:
                print(f"❌ Failed: {e}\n")
                traceback.print_exc()
                errors.append(f"{display}: {type(e).__name__}: {e}")
            finally:
                if not args.dry_run:
                    save_state(state)
    except Exception as e:
        traceback.print_exc()
        errors.append(f"{type(e).__name__}: {e}")

    if not args.dry_run:
        send_report(results, errors)

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())