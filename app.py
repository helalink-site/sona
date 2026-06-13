import os, json, hashlib, re, subprocess, logging
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, Response, stream_with_context
from flask_cors import CORS
import requests

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('sona')
app = Flask(__name__)
CORS(app, origins=['*'])
CACHE_DIR = Path(os.getenv('CACHE_DIR', '/tmp/sona_cache'))
PORT = int(os.getenv('PORT', 5000))
YT_API_KEY = os.getenv('YOUTUBE_API_KEY', '')
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

    tracks = []

    # YouTube Data API v3 - no bot detection
    if YT_API_KEY:
        try:
            r = requests.get('https://www.googleapis.com/youtube/v3/search', params={
                'part': 'snippet',
                'q': query,
                'type': 'video',
                'videoCategoryId': '10',  # Music category
                'maxResults': limit,
                'key': YT_API_KEY,
            }, timeout=10)
            data = r.json()
            items = data.get('items', [])
            # Get durations via videos endpoint
            ids = ','.join([i['id']['videoId'] for i in items if i.get('id',{}).get('videoId')])
            dur_map = {}
            if ids:
                vr = requests.get('https://www.googleapis.com/youtube/v3/videos', params={
                    'part': 'contentDetails',
                    'id': ids,
                    'key': YT_API_KEY,
                }, timeout=10)
                for v in vr.json().get('items', []):
                    dur = v.get('contentDetails', {}).get('duration', '')
                    # Parse ISO 8601 duration PT3M45S
                    import re as re2
                    m = re2.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', dur)
                    if m:
                        h, mn, s = (int(x or 0) for x in m.groups())
                        total = h*3600 + mn*60 + s
                        mins2, secs = divmod(total, 60)
                        dur_map[v['id']] = f'{mins2}:{secs:02d}'

            for item in items:
                vid = item.get('id', {}).get('videoId', '')
                if not vid: continue
                snippet = item.get('snippet', {})
                thumb = snippet.get('thumbnails', {}).get('high', {}).get('url') or \
                        f'https://img.youtube.com/vi/{vid}/mqdefault.jpg'
                tracks.append({
                    'videoId': vid,
                    'title': snippet.get('title', 'Unknown'),
                    'artist': snippet.get('channelTitle', 'Unknown').replace(' - Topic', ''),
                    'thumbnail': thumb,
                    'duration': dur_map.get(vid, ''),
                    'url': f'https://youtube.com/watch?v={vid}',
                })
        except Exception as e:
            log.error(f'YouTube API error: {e}')

    if tracks:
        cache_set(key, tracks)
    return tracks

def get_audio_url(video_id):
    key = f'audio:{video_id}'
    cached = cache_get(key)
    if cached: return cached
    try:
        cmd = ['yt-dlp', f'https://youtube.com/watch?v={video_id}',
               '--get-url', '-f', 'bestaudio[ext=m4a]/bestaudio/best',
               '--no-warnings', '--quiet']
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        url = r.stdout.strip().split('\n')[0]
        if url and url.startswith('http'):
            cache_set(key, url, mins=4)
            return url
    except Exception as e:
        log.error(f'Audio URL error: {e}')
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
        url = get_audio_url(video_id)
        if not url:
            return jsonify({'error': 'Could not get audio URL'}), 404
        headers = {
            'User-Agent': 'Mozilla/5.0',
            'Range': request.headers.get('Range', 'bytes=0-')
        }
        r = requests.get(url, headers=headers, stream=True, timeout=10)
        def generate():
            for chunk in r.iter_content(chunk_size=8192):
                if chunk: yield chunk
        resp_headers = {
            'Content-Type': r.headers.get('Content-Type', 'audio/mp4'),
            'Accept-Ranges': 'bytes',
        }
        if 'Content-Length' in r.headers:
            resp_headers['Content-Length'] = r.headers['Content-Length']
        if 'Content-Range' in r.headers:
            resp_headers['Content-Range'] = r.headers['Content-Range']
        return Response(stream_with_context(generate()),
                       status=r.status_code, headers=resp_headers)
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
        return send_file(str(out), as_attachment=True,
                        download_name=f'{safe}.mp3', mimetype='audio/mpeg')
    return jsonify({'error': 'Download failed'}), 500

@app.route('/api/artists')
def artists():
    q = request.args.get('q', '')
    tracks = search_tracks(f'{q} official music', 5)
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
