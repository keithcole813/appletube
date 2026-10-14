#!/usr/bin/env python3
"""
AppleTube - Upload an Apple playlist, get YouTube links for closest matches,
download individual songs or everything as a ZIP.
"""
import os
import re
import json
import zipfile
import tempfile
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from flask import Flask, request, jsonify, render_template, send_file

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB max upload

# Cache for YouTube search results (query -> video info)
search_cache = {}

def parse_txt_playlist(content):
    """Parse plain text playlist - one song per line, format: Artist - Title or Title by Artist"""
    songs = []
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # Try "Artist - Title" format
        if ' - ' in line:
            parts = line.split(' - ', 1)
            songs.append({'artist': parts[0].strip(), 'title': parts[1].strip()})
        elif ' by ' in line.lower():
            parts = re.split(r'\s+by\s+', line, 1, flags=re.I)
            songs.append({'artist': parts[1].strip(), 'title': parts[0].strip()})
        else:
            songs.append({'artist': '', 'title': line})
    return songs

def parse_m3u_playlist(content):
    """Parse M3U playlist"""
    songs = []
    for line in content.split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        # M3U often has file paths - extract filename
        filename = Path(line).stem
        # Remove common suffixes
        filename = re.sub(r'\.(mp3|m4a|wav|flac|aac)$', '', filename, flags=re.I)
        if ' - ' in filename:
            parts = filename.split(' - ', 1)
            songs.append({'artist': parts[0].strip(), 'title': parts[1].strip()})
        else:
            songs.append({'artist': '', 'title': filename})
    return songs

def parse_itunes_xml(content):
    """Parse iTunes/Music app XML playlist"""
    songs = []
    try:
        root = ET.fromstring(content)
        # iTunes XML structure: plist > dict > dict (Tracks)
        for dict_elem in root.findall('.//dict'):
            track = {}
            keys = [k.text for k in dict_elem.findall('key')]
            vals = dict_elem.findall('string')
            # Simplified: look for Name and Artist
            d = {}
            children = list(dict_elem)
            for i, child in enumerate(children):
                if child.tag == 'key' and i + 1 < len(children):
                    d[child.text] = children[i + 1].text
            if 'Name' in d:
                songs.append({
                    'artist': d.get('Artist', ''),
                    'title': d.get('Name', '')
                })
    except Exception as e:
        print(f"XML parse error: {e}")
    # Deduplicate (XML has nested dicts)
    seen = set()
    unique = []
    for s in songs:
        key = (s['artist'], s['title'])
        if key not in seen and s['title']:
            seen.add(key)
            unique.append(s)
    return unique

def search_youtube(artist, title):
    """Search YouTube for closest match using yt-dlp"""
    query = f"{artist} {title}".strip()
    if query in search_cache:
        return search_cache[query]
    
    try:
        # Use yt-dlp to search and get first result
        cmd = [
            'yt-dlp',
            f'ytsearch1:{query}',
            '--print', '%(id)s|%(title)s|%(duration_string)s|%(thumbnail)s',
            '--no-playlist',
            '--quiet'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and result.stdout.strip():
            parts = result.stdout.strip().split('|')
            if len(parts) >= 2:
                info = {
                    'video_id': parts[0],
                    'title': parts[1],
                    'duration': parts[2] if len(parts) > 2 else '',
                    'thumbnail': parts[3] if len(parts) > 3 else '',
                    'url': f'https://youtube.com/watch?v={parts[0]}',
                    'query': query
                }
                search_cache[query] = info
                return info
    except Exception as e:
        print(f"Search error for '{query}': {e}")
    
    return {'video_id': '', 'title': 'Not found', 'url': '', 'query': query}

def download_audio(video_id, output_dir):
    """Download YouTube video as MP3"""
    try:
        cmd = [
            'yt-dlp',
            f'https://youtube.com/watch?v={video_id}',
            '-x', '--audio-format', 'mp3', '--audio-quality', '0',
            '-o', os.path.join(output_dir, '%(title)s.%(ext)s'),
            '--no-playlist',
            '--quiet'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        return result.returncode == 0
    except Exception as e:
        print(f"Download error for {video_id}: {e}")
        return False

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    """Handle playlist file upload and return parsed songs"""
    if 'file' not in request.files:
        return jsonify({'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'No file selected'}), 400
    
    filename = file.filename.lower()
    content = file.read().decode('utf-8', errors='ignore')
    
    if filename.endswith('.xml') or filename.endswith('.plist'):
        songs = parse_itunes_xml(content)
    elif filename.endswith('.m3u') or filename.endswith('.m3u8'):
        songs = parse_m3u_playlist(content)
    else:  # .txt or other
        songs = parse_txt_playlist(content)
    
    # Add IDs for frontend tracking
    for i, song in enumerate(songs):
        song['id'] = i
    
    return jsonify({'songs': songs, 'count': len(songs)})

@app.route('/search', methods=['POST'])
def search():
    """Search YouTube for a song"""
    data = request.json
    artist = data.get('artist', '')
    title = data.get('title', '')
    result = search_youtube(artist, title)
    return jsonify(result)

@app.route('/search_batch', methods=['POST'])
def search_batch():
    """Search YouTube for multiple songs"""
    data = request.json
    songs = data.get('songs', [])
    results = []
    for song in songs:
        result = search_youtube(song.get('artist', ''), song.get('title', ''))
        result['song_id'] = song.get('id')
        results.append(result)
    return jsonify({'results': results})

@app.route('/download/<video_id>')
def download_single(video_id):
    """Download single song as MP3"""
    with tempfile.TemporaryDirectory() as tmpdir:
        if download_audio(video_id, tmpdir):
            files = list(Path(tmpdir).glob('*.mp3'))
            if files:
                return send_file(str(files[0]), as_attachment=True, download_name=files[0].name)
    return jsonify({'error': 'Download failed'}), 500

@app.route('/download_zip', methods=['POST'])
def download_zip():
    """Download multiple songs as ZIP"""
    data = request.json
    video_ids = data.get('video_ids', [])
    
    if not video_ids:
        return jsonify({'error': 'No songs selected'}), 400
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download all
        for vid in video_ids:
            download_audio(vid, tmpdir)
        
        # Create ZIP
        zip_path = os.path.join(tmpdir, 'appletube_playlist.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for mp3 in Path(tmpdir).glob('*.mp3'):
                zf.write(mp3, mp3.name)
        
        return send_file(zip_path, as_attachment=True, download_name='appletube_playlist.zip')

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5004, debug=False)
