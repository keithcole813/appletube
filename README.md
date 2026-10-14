# AppleTube 🍎📺

Upload an Apple playlist, get YouTube links for the closest matches, download individual MP3s or everything as a ZIP.

## Features
- Upload `.txt`, `.m3u`, or iTunes `.xml` playlists
- Auto-searches YouTube for closest match per song
- Preview matches with thumbnails before downloading
- Remove songs from the list before downloading
- Download individual MP3s or all as a ZIP

## Run
```bash
pip install -r requirements.txt
python app.py
```
Runs on `http://0.0.0.0:5004`

## Playlist formats
- **TXT**: One song per line — `Artist - Title` or just `Title`
- **M3U**: Standard M3U, filenames parsed for artist/title
- **XML**: iTunes/Music app exported playlist XML
