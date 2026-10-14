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
import urllib.request
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
    
    # Use venv yt-dlp if available, else system
    import sys
    venv_ytdlp = str(Path(__file__).parent / 'venv' / 'bin' / 'yt-dlp')
    ytdlp_bin = venv_ytdlp if Path(venv_ytdlp).exists() else 'yt-dlp'
    
    try:
        # Use yt-dlp to search and get first result
        cmd = [
            ytdlp_bin,
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
    venv_ytdlp = str(Path(__file__).parent / 'venv' / 'bin' / 'yt-dlp')
    ytdlp_bin = venv_ytdlp if Path(venv_ytdlp).exists() else 'yt-dlp'
    try:
        cmd = [
            ytdlp_bin,
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

@app.route('/yt_search_batch', methods=['POST', 'OPTIONS'])
def yt_search_batch():
    """Batch YouTube search - parallel yt-dlp for speed"""
    if request.method == 'OPTIONS':
        resp = jsonify({})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    
    from concurrent.futures import ThreadPoolExecutor
    data = request.json
    songs = data.get('songs', [])  # [{artist, title, id}]
    
    def search_one(song):
        result = search_youtube(song.get('artist',''), song.get('title',''))
        return {
            'id': song.get('id'),
            'videoId': result.get('video_id',''),
            'title': result.get('title',''),
            'url': result.get('url',''),
            'thumb': result.get('thumbnail',''),
            'duration': result.get('duration','')
        }
    
    with ThreadPoolExecutor(max_workers=8) as ex:
        results = list(ex.map(search_one, songs))
    
    resp = jsonify({'results': results})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/yt_search', methods=['POST', 'OPTIONS'])
def yt_search():
    """Search YouTube via yt-dlp (server-side, reliable)"""
    if request.method == 'OPTIONS':
        resp = jsonify({})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    
    data = request.json
    artist = data.get('artist', '').strip()
    title = data.get('title', '').strip()
    result = search_youtube(artist, title)
    # Normalize to browser format
    out = {
        'videoId': result.get('video_id', ''),
        'title': result.get('title', ''),
        'url': result.get('url', ''),
        'thumb': result.get('thumbnail', ''),
        'duration': result.get('duration', '')
    }
    resp = jsonify(out)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/fetch_url', methods=['POST', 'OPTIONS'])
def fetch_url():
    """Fetch Apple Music playlist from URL (server-side, no CORS issues)"""
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        resp = jsonify({})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    
    data = request.json
    url = data.get('url', '').strip()
    if not url:
        resp = jsonify({'error': 'No URL provided'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 400
    
    try:
        # Fetch Apple Music page server-side
        req = urllib.request.Request(url, headers={
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        })
        with urllib.request.urlopen(req, timeout=30) as resp:
            html = resp.read().decode('utf-8', errors='ignore')
        
        # Parse tracks: "artistName":"...",...,"name":"..."
        songs = []
        seen = set()
        pat = re.compile(r'"artistName"\s*:\s*"([^"]+)"[\s\S]{0,500}?"name"\s*:\s*"([^"]+)"')
        for m in pat.finditer(html):
            artist, title = m.group(1), m.group(2)
            if len(title) > 100:
                continue
            key = artist + '|' + title
            if key not in seen:
                seen.add(key)
                songs.append({'artist': artist, 'title': title})
        
        if not songs:
            resp = jsonify({'error': 'No tracks found in playlist'})
            resp.headers['Access-Control-Allow-Origin'] = '*'
            return resp, 404
        
        for i, song in enumerate(songs):
            song['id'] = i
        
        resp = jsonify({'songs': songs, 'count': len(songs)})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    except Exception as e:
        resp = jsonify({'error': f'Failed to fetch playlist: {str(e)}'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 500

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

@app.route('/download_zip', methods=['POST', 'OPTIONS'])
def download_zip():
    """Download multiple songs as ZIP (with CORS)"""
    if request.method == 'OPTIONS':
        resp = jsonify({})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    data = request.json
    video_ids = data.get('video_ids', [])
    
    if not video_ids:
        resp = jsonify({'error': 'No songs selected'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 400
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Download all
        for vid in video_ids:
            download_audio(vid, tmpdir)
        
        # Create ZIP
        zip_path = os.path.join(tmpdir, 'appletube_playlist.zip')
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            for mp3 in Path(tmpdir).glob('*.mp3'):
                zf.write(mp3, mp3.name)
        
        resp = send_file(zip_path, as_attachment=True, download_name='appletube_playlist.zip')
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp

# Background ZIP jobs (for large playlists that would timeout)
zip_jobs = {}

@app.route('/zip_start', methods=['POST', 'OPTIONS'])
def zip_start():
    """Start background ZIP job, return job_id immediately"""
    if request.method == 'OPTIONS':
        resp = jsonify({})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Access-Control-Allow-Methods'] = 'POST, OPTIONS'
        resp.headers['Access-Control-Allow-Headers'] = 'Content-Type'
        return resp
    import uuid, threading
    data = request.json
    video_ids = data.get('video_ids', [])
    if not video_ids:
        resp = jsonify({'error': 'No songs selected'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 400
    
    job_id = str(uuid.uuid4())[:8]
    job_dir = tempfile.mkdtemp(prefix=f'appletube_{job_id}_')
    zip_jobs[job_id] = {'status': 'starting', 'done': 0, 'total': len(video_ids), 'dir': job_dir, 'zip_path': None}
    
    def run():
        job = zip_jobs[job_id]
        job['status'] = 'downloading'
        for vid in video_ids:
            if job.get('cancelled'): break
            download_audio(vid, job_dir)
            job['done'] += 1
        if not job.get('cancelled'):
            job['status'] = 'zipping'
            zip_path = os.path.join(job_dir, 'appletube_playlist.zip')
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
                for mp3 in Path(job_dir).glob('*.mp3'):
                    zf.write(mp3, mp3.name)
            job['zip_path'] = zip_path
            job['status'] = 'done'
    
    threading.Thread(target=run, daemon=True).start()
    resp = jsonify({'job_id': job_id, 'total': len(video_ids)})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/zip_status/<job_id>', methods=['GET', 'OPTIONS'])
def zip_status(job_id):
    if request.method == 'OPTIONS':
        resp = jsonify({})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp
    job = zip_jobs.get(job_id)
    if not job:
        resp = jsonify({'error': 'Job not found'})
        resp.headers['Access-Control-Allow-Origin'] = '*'
        return resp, 404
    resp = jsonify({'status': job['status'], 'done': job['done'], 'total': job['total']})
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

@app.route('/zip_result/<job_id>', methods=['GET'])
def zip_result(job_id):
    job = zip_jobs.get(job_id)
    if not job or job['status'] != 'done' or not job['zip_path']:
        return jsonify({'error': 'Not ready'}), 404
    resp = send_file(job['zip_path'], as_attachment=True, download_name='appletube_playlist.zip')
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5004, debug=False)
