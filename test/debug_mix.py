import os
from pathlib import Path

from ytmusicapi import YTMusic


def _auth_path():
    data_home = os.environ.get("XDG_DATA_HOME")
    if not data_home:
        data_home = str(Path.home() / ".local" / "share")
    return str(Path(data_home) / "ventapes" / "headers_auth.json")


yt = YTMusic(_auth_path())  # Use auth if available for personal mixes

# Fetch HOME to find mixes
print("\n--- FETCHING HOME ---")
mix_id = None
try:
    home = yt.get_home()
    for item in home:
        # Check sections
        if 'contents' in item:
            for c in item['contents']:
                pid = c.get('playlistId')
                if pid and pid.startswith("RDTMAK"):
                    print(f"Found Mix: {c.get('title')} - ID: {pid}")
                    mix_id = pid
                    break
        if mix_id: break
except Exception as e:
    print(f"Error fetching home: {e}")

if mix_id:
    print(f"\n--- FETCHING PLAYLIST: {mix_id} ---")
    try:
        playlist = yt.get_playlist(mix_id)
        # print(json.dumps(playlist, indent=2)) 
        # Just check keys and first track
        print("Keys:", playlist.keys())
        if 'tracks' in playlist:
            print(f"Tracks count: {len(playlist['tracks'])}")
            if len(playlist['tracks']) > 0:
                print("First track:", playlist['tracks'][0])
    except Exception as e:
        print(f"ERROR fetching playlist: {e}")
else:
    print("Could not find RDTMAK in Home.")
