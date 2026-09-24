import os
from pathlib import Path

from ytmusicapi import YTMusic


def _auth_path():
    data_home = os.environ.get("XDG_DATA_HOME")
    if not data_home:
        data_home = str(Path.home() / ".local" / "share")
    return str(Path(data_home) / "ventapes" / "headers_auth.json")


yt = YTMusic(_auth_path())

# Helper to find mix
mix_id = None
try:
    home = yt.get_home()
    for item in home:
        if 'contents' in item:
            for c in item['contents']:
                pid = c.get('playlistId')
                if pid and pid.startswith("RDTMAK"):
                    mix_id = pid
                    break
        if mix_id: break
except: pass

if not mix_id:
    print("No Mix found, using a fallback or skipping.")
    # Fallback to a search result for testing limits if needed, but RDTMAK is key.
    exit()

print(f"Testing Mix: {mix_id}")

print("--- Fetching Limit=10 ---")
res1 = yt.get_playlist(mix_id, limit=10)
tracks1 = res1.get('tracks', [])
print(f"Tracks: {len(tracks1)}")
if tracks1:
    print(f"Track 0: {tracks1[0]['title']}")
    print(f"Track 9: {tracks1[-1]['title']}")

print("\n--- Fetching Limit=150 ---")
res3 = yt.get_playlist(mix_id, limit=150)
tracks3 = res3.get('tracks', [])
print(f"Tracks: {len(tracks3)}")
