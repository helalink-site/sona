import os, json, hashlib, re, subprocess, logging
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file
from flask_cors import CORS

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('sona')
app = Flask(__name__)
CORS(app, origins=['*'])
CACHE_DIR = Path(os.getenv('CACHE_DIR', '/tmp/sona_cache'))
PORT = int(os.getenv('PORT', 5000))
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_cache = {}

def cache_get(key):
    if key in _cache:
        data, exp = _cache[key]
        if datetime.now() < exp: return data
        del _cache[key]
    return None

def cache_set(key, data, mins=30):
    _cache[key] = (data, datetime.now() + timedelta(minutes=mins))

def search_tracks(query, limit=10):
    key = hashlib.md5(f'{query}:{limit}'.encode()).hexdigest()
    cached = cache_get(key)
    if cached: return cached
    try:
        cmd = ['yt-dlp', f'ytsearch{limit}:{query}', '--flat-playlist',
               '--print', '%(id)s|%(title)s|%(uploader)s|%(duration)s',
               '--no-warnings', '--quiet']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        tracks = []
        for line in r.stdout.strip().split('\n'):
            if not line.strip(): continue
            p = line.split('|')
            if len(p) < 3: continue
            vid = p[0].strip()
            try:
                d = int(p[3].strip()) if len(p) > 3 else 0
                m, s = divmod(d, 60)
                dur = f'{m}:{s:02d}'
            except: dur = ''
            tracks.append({
                'videoId': vid,
                'title': p[1].strip(),
                'artist': p[2].strip().replace(' - Topic',''),
                'thumbnail': f'https://img.youtube.com/vi/{vid}/mqdefault.jpg',
                'duration': dur,
                'url': f'https://youtube.com/watch?v={vid}',
            })
        if tracks: cache_set(key, tracks)
        return tracks
    except Exception as e:
        log.error(f'Search error: {e}')
        return []

def get_stream_url(video_id):
    key = f'stream:{video_id}'
    cached = cache_get(key)
    if cached: return cached
    try:
        cmd = ['yt-dlp', f'https://youtube.com/watch?v={video_id}',
               '--get-url', '-f', 'bestaudio[ext=m4a]/bestaudio/best',
               '--no-warnings', '--quiet']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        url = r.stdout.strip().split('\n')[0]
        if url and url.startswith('http'):
            cache_set(key, url, mins=5)
            return url
    except Exception as e:
        log.error(f'Stream error: {e}')
    return ''

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Sona', 'version': '2.0'})

@app.route('/api/search')
def search():
    q = request.args.get('q', '').strip()
    limit = min(int(request.args.get('limit', 10)), 20)
    if not q: return jsonify({'tracks': [], 'error': 'Query required'}), 400
    tracks = search_tracks(q, limit)
    return jsonify({'tracks': tracks, 'query': q, 'count': len(tracks)})

@app.route('/api/stream/<video_id>')
def stream(video_id):
    try:
        url = get_stream_url(video_id)
        if not url: return jsonify({'error': 'Could not get stream URL'}), 404
        return jsonify({'url': url, 'videoId': video_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<video_id>')
def download(video_id):
    title = request.args.get('title', video_id)
    safe = re.sub(r'[^\w\s-]', '', title)[:50]
    out = CACHE_DIR / f'{video_id}.mp3'
    if not out.exists():
        try:
            subprocess.run([
                'yt-dlp', f'https://youtube.com/watch?v={video_id}',
                '-x', '--audio-format', 'mp3', '--audio-quality', '0',
                '-o', str(CACHE_DIR / f'{video_id}.%(ext)s'),
                '--no-warnings', '--quiet'
            ], timeout=120)
        except Exception as e:
            return jsonify({'error': str(e)}), 500
    if out.exists():
        return send_file(str(out), as_attachment=True, download_name=f'{safe}.mp3', mimetype='audio/mpeg')
    return jsonify({'error': 'Download failed'}), 500

@app.route('/api/artists')
def artists():
    q = request.args.get('q', '')
    tracks = search_tracks(f'{q} official', 5)
    names = list(dict.fromkeys([t['artist'] for t in tracks if t['artist']]))
    return jsonify({'artists': names})

@app.route('/api/subscribe', methods=['POST'])
def subscribe():
    return jsonify({'success': True})

@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    frontend = Path('frontend')
    f = frontend / path
    if path and f.exists() and f.is_file():
        return send_file(str(f))
    return send_file(str(frontend / 'index.html'))

if __name__ == '__main__':
    log.info(f'Sona starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
