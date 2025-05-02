# Hello there
`playlist-import.py` is a Python script used to import Spotify playlists to Youtube Music. The current iteration only imports user-created playlists.

## Pre-requisites
- [Spotify API](https://developer.spotify.com/)
- [Youtube Music API](https://ytmusicapi.readthedocs.io/en/stable/)
- [Python](https://www.python.org/downloads/)

### Setup

For a cleaner setup, let's create a virtual environment:

```
# Create a virtual environment
python -m venv venv

# Activate virtual environment
venv\bin\activate

# Install required packages
pip install -r requirements.txt
```

After finishing the setup of the developer accounts and installing Python/virtual environment. Here are some crucial values needed for this script to work:

Youtube Music API
- Client ID
- Client Secret
- `oauth.json` - Additional instructions [here](#obtaining-youtube-music-api-token)

Spotify Developer API
- Client ID
- Client Secret
- Redirect URI
- `.cache` - Additional instructions [here](#obtaining-spotify-token)

#### Obtaining Youtube Music API token
For `oath.json`

> Although this step should've been part of the setup process, this is an important step for the script to work.

```
Run ytmusicapi oauth

Enter your `client-id`

Enter your `client-secret`

A browser will open and a generated code will show up.

Copy the code and then proceed.

Proceed until you finish

Come back to terminal and press Enter

oauth.json should be generated afterwards
```

> If the user is not allowed or was rejected, verify if user or email selected was added as test user for the project.


#### Obtaining Spotify token
For `.cache`

```
Run playlist-import.py

Browser will open using redirect-uri

Copy uri that opens in browser and enter in terminal

.cache should have been generated
```

