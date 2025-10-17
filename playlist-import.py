# Import libraries
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from ytmusicapi import YTMusic
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Retrieve values from .env
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')

# Initialize Spotify API client
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope='playlist-read-private'
))

# Initialize YouTube Music API with headers authentication
# This uses browser cookies and doesn't expire like OAuth
ytmusic = YTMusic('browser.json')

def get_spotify_playlists():
    # Get current user's ID to filter playlists
    current_user = sp.current_user()
    user_id = current_user['id']

    playlists = sp.current_user_playlists()
    # Check playlist if user created
    user_playlists = [p for p in playlists['items'] if p['owner']['id'] == user_id]
    return user_playlists

# Retrieve YouTube Music playlists
def get_existing_ytmusic_playlists():
    try:
        playlists = ytmusic.get_library_playlists(limit=None)
        return playlists if playlists else []
    except Exception as e:
        print(f"Warning: Could not fetch YouTube Music playlists: {str(e)}")
        print("Will proceed without duplicate checking")
        return []

# Playlist name check
def playlist_exists(playlist_name, existing_playlists):
    return any(p['title'].lower() == playlist_name.lower() for p in existing_playlists)

# Get Spotify playlist tracks with pagination support
def transfer_playlist(spotify_playlist_id, youtube_playlist_name):
    tracks = []
    results = sp.playlist_tracks(spotify_playlist_id)
    tracks.extend(results['items'])

    # Handle pagination - get all tracks if playlist has more than 100 songs
    while results['next']:
        results = sp.next(results)
        tracks.extend(results['items'])

    print(f"  Found {len(tracks)} tracks in Spotify playlist")

    # Search for each track on YouTube Music
    video_ids = []
    not_found = []

    for i, track in enumerate(tracks, 1):
        if track['track'] is None:  # Handle deleted/unavailable tracks
            continue

        track_name = track['track']['name']
        artist = track['track']['artists'][0]['name']

        print(f"  Searching {i}/{len(tracks)}: {track_name} - {artist}")

        # Search YouTube Music
        try:
            search_results = ytmusic.search(f"{track_name} {artist}", filter="songs")
            if search_results:
                video_id = search_results[0]['videoId']
                video_ids.append(video_id)
            else:
                not_found.append(f"{track_name} - {artist}")
        except Exception as e:
            print(f"    Error searching: {str(e)}")
            not_found.append(f"{track_name} - {artist}")

    print(f"  Successfully found {len(video_ids)}/{len(tracks)} tracks on YouTube Music")

    if not_found:
        print(f"  Could not find {len(not_found)} tracks:")
        for track in not_found[:5]:  # Show first 5
            print(f"    - {track}")
        if len(not_found) > 5:
            print(f"    ... and {len(not_found) - 5} more")

    # Create YouTube Music playlist
    try:
        playlist_id = ytmusic.create_playlist(youtube_playlist_name, "Imported from Spotify")
    except Exception as e:
        print(f"  Error creating playlist (trying with sanitized name): {str(e)}")
        # If playlist creation fails, try with a sanitized name
        sanitized_name = youtube_playlist_name.encode('ascii', 'ignore').decode('ascii')
        if not sanitized_name.strip():
            sanitized_name = "Imported Playlist"
        playlist_id = ytmusic.create_playlist(sanitized_name, "Imported from Spotify")

    # Add tracks to playlist (YouTube Music has a limit, so batch if needed)
    if video_ids:
        # Add in batches of 100 (API limitation)
        for i in range(0, len(video_ids), 100):
            batch = video_ids[i:i+100]
            ytmusic.add_playlist_items(playlist_id, batch)
            print(f"  Added batch {i//100 + 1} ({len(batch)} tracks)")

    return playlist_id

def main():
    spotify_playlists = get_spotify_playlists()

    print(f"Found {len(spotify_playlists)} playlists to process\n")

    for playlist in spotify_playlists:
        playlist_name = playlist['name']

        # Check if playlist exists on YouTube Music (refresh each time)
        existing_yt_playlists = get_existing_ytmusic_playlists()
        if playlist_exists(playlist_name, existing_yt_playlists):
            print(f"⏭️  Skipping '{playlist_name}' - already exists in YouTube Music")
            continue

        print(f"▶️  Transferring playlist: {playlist_name}")
        try:
            transfer_playlist(playlist['id'], playlist_name)
            print(f"✅ Successfully transferred '{playlist_name}'\n")
        except Exception as e:
            print(f"❌ Failed to transfer '{playlist_name}': {str(e)}\n")

if __name__ == "__main__":
    main()