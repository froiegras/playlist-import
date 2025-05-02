# Import libraries
import spotipy
from spotipy.oauth2 import SpotifyOAuth
from ytmusicapi import YTMusic, OAuthCredentials
from dotenv import load_dotenv
import os

# Load environment variables from .env file
load_dotenv()

# Retrieve values from .env
SPOTIFY_CLIENT_ID = os.getenv('SPOTIFY_CLIENT_ID')
SPOTIFY_CLIENT_SECRET = os.getenv('SPOTIFY_CLIENT_SECRET')
SPOTIFY_REDIRECT_URI = os.getenv('SPOTIFY_REDIRECT_URI')
YTMUSIC_CLIENT_ID = os.getenv('YTMUSIC_CLIENT_ID')
YTMUSIC_CLIENT_SECRET = os.getenv('YTMUSIC_CLIENT_SECRET')

# Initialize Spotify API client
sp = spotipy.Spotify(auth_manager=SpotifyOAuth(
    client_id=SPOTIFY_CLIENT_ID,
    client_secret=SPOTIFY_CLIENT_SECRET,
    redirect_uri=SPOTIFY_REDIRECT_URI,
    scope='playlist-read-private'
))

# Initialize YouTube Music API
ytmusic = YTMusic('oauth.json', oauth_credentials=OAuthCredentials(
    client_id=YTMUSIC_CLIENT_ID,
    client_secret=YTMUSIC_CLIENT_SECRET
))

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
    return ytmusic.get_library_playlists()

# Playlist name check
def playlist_exists(playlist_name, existing_playlists):
    return any(p['title'].lower() == playlist_name.lower() for p in existing_playlists)

# Get Spotify playlist tracks
def transfer_playlist(spotify_playlist_id, youtube_playlist_name):
    results = sp.playlist_tracks(spotify_playlist_id)
    tracks = results['items']

    # Search for each track on YouTube Music
    video_ids = []
    for track in tracks:
        track_name = track['track']['name']
        artist = track['track']['artists'][0]['name']

        # Search YouTube Music
        search_results = ytmusic.search(f"{track_name} {artist}", filter="songs")
        if search_results:
            video_id = search_results[0]['videoId']
            video_ids.append(video_id)

    # Create YouTube Music playlist
    playlist_id = ytmusic.create_playlist(youtube_playlist_name, "Imported from Spotify")

    # Add tracks to playlist
    if video_ids:
        ytmusic.add_playlist_items(playlist_id, video_ids)

    return playlist_id

def main():
    existing_yt_playlists = get_existing_ytmusic_playlists()

    spotify_playlists = get_spotify_playlists()

    for playlist in spotify_playlists:
        playlist_name = playlist['name']

        if playlist_exists(playlist_name, existing_yt_playlists):
            print(f"Skipping '{playlist_name}' - already exists in YouTube Music")
            continue

        print(f"Transferring playlist: {playlist_name}")
        try:
            transfer_playlist(playlist['id'], playlist_name)
            print(f"Successfully transferred {playlist_name}")
        except Exception as e:
            print(f"Failed to transfer {playlist_name}: {str(e)}")

if __name__ == "__main__":
    main()