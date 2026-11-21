import argparse
import json
import os
import re
from pathlib import Path

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

EXCLUDED_PLAYLISTS = os.getenv("EXCLUDED_PLAYLISTS", "")

SCOPE = (
    "playlist-read-private playlist-read-collaborative "
    "playlist-modify-public playlist-modify-private"
)

STATE_FILE = Path("sync_state.json")
BATCH = 100

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
    """name(lower) -> {'id', 'name'} for playlists owned by the current user."""
    playlists = sp_paginate(sp.current_user_playlists(limit=50))
    return {
        p["name"].lower(): {"id": p["id"], "name": p["name"]}
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


def create_spotify_playlist(name):
    pl = sp.user_playlist_create(SP_USER_ID, name, public=False, description="")
    return pl["id"]


def add_to_spotify(playlist_id, track_ids):
    for i in range(0, len(track_ids), BATCH):
        sp.playlist_add_items(playlist_id, [f"spotify:track:{t}" for t in track_ids[i:i + BATCH]])


def get_ytmusic_playlists(excluded=frozenset()):
    """name(lower) -> {'id', 'name', 'tracks'} for playlists owned by the user."""
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
        result[p["title"].lower()] = {"id": pid, "name": p["title"], "tracks": dedupe(tracks)}
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


def create_ytmusic_playlist(name):
    try:
        return ytmusic.create_playlist(name, "", privacy_status="PRIVATE")
    except Exception as e:
        print(f"  Error creating playlist (retrying with sanitized name): {e}")
        sanitized = re.sub(r"[<>]", "", name).strip() or "Imported Playlist"
        return ytmusic.create_playlist(sanitized, "", privacy_status="PRIVATE")


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
    """Search the other service for each missing track. Returns list of found ids.

    If the search lands on a track that is already in the target playlist (e.g. the
    title/artist text differs between services, as with romanized vs. native names),
    we record the match but don't add a duplicate.
    """
    found = []
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
                found.append(other_id)
            else:
                print("      Already in target playlist under a different name, skipping")
        else:
            not_found_list.append(t["key"])
            print("      Not found")
    return list(dict.fromkeys(found))


def sync_playlist(name, sp_pl, yt_pl, state, dry_run):
    if sp_pl is None:
        print("  Playlist only on YouTube Music -> creating on Spotify")
        sp_pl = {"id": None if dry_run else create_spotify_playlist(name), "name": name}
        sp_tracks = []
    else:
        sp_tracks = get_spotify_tracks(sp_pl["id"])

    if yt_pl is None:
        print("  Playlist only on Spotify -> creating on YouTube Music")
        yt_pl = {"id": None if dry_run else create_ytmusic_playlist(name), "name": name, "tracks": []}
    yt_tracks = yt_pl["tracks"]

    print(f"  Spotify: {len(sp_tracks)} tracks | YouTube Music: {len(yt_tracks)} tracks")

    add_to_yt = tracks_to_add(sp_tracks, yt_tracks, state["sp_to_yt"], set(state["not_found_on_yt"]))
    add_to_sp = tracks_to_add(yt_tracks, sp_tracks, state["yt_to_sp"], set(state["not_found_on_sp"]))

    if add_to_yt:
        print(f"  {len(add_to_yt)} track(s) to add to YouTube Music")
        video_ids = resolve(add_to_yt, find_on_youtube, state, "sp_to_yt", "yt_to_sp",
                            state["not_found_on_yt"], {t["id"] for t in yt_tracks})
        if video_ids and not dry_run:
            add_to_ytmusic(yt_pl["id"], video_ids)
        print(f"  {'Would add' if dry_run else 'Added'} {len(video_ids)} to YouTube Music")

    if add_to_sp:
        print(f"  {len(add_to_sp)} track(s) to add to Spotify")
        spotify_ids = resolve(add_to_sp, find_on_spotify, state, "yt_to_sp", "sp_to_yt",
                              state["not_found_on_sp"], {t["id"] for t in sp_tracks})
        if spotify_ids and not dry_run:
            add_to_spotify(sp_pl["id"], spotify_ids)
        print(f"  {'Would add' if dry_run else 'Added'} {len(spotify_ids)} to Spotify")

    if not add_to_yt and not add_to_sp:
        print("  Already in sync")


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

    state = load_state()
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
            sync_playlist(display, sp_pl, yt_pl, state, args.dry_run)
            print("✅ Done\n")
        except Exception as e:
            print(f"❌ Failed: {e}\n")
        finally:
            if not args.dry_run:
                save_state(state)


if __name__ == "__main__":
    main()