import os, json, hashlib, re, subprocess, logging, random
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

PIPED_INSTANCES = [
    'https://pipedapi.kavin.rocks',
    'https://pipedapi.adminforge.de',
    'https://piped-api.garudalinux.org',
    'https://api.piped.yt',
    'https://pipedapi.in.projectsegfau.lt',
]

INVIDIOUS_INSTANCES = [
    'https://invidious.nerdvpn.de',
    'https://inv.nadeko.net',
    'https://invidious.privacyredirect.com',
    'https://invidious.fdn.fr',
    'https://vid.puffyan.us',
]

def cache_get(key):
    if key in _cache:
        data, exp = _cache[key]
        if datetime.now() < exp: return data
        del _cache[key]
    return None

def cache_set(key, data, mins=30):
    _cache[key] = (data, datetime.now() + timedelta(minutes=mins))

# ── SEARCH ────────────────────────────────────────────────────────────────────
def search_tracks(query, limit=10):
    key = hashlib.md5(f'{query}:{limit}'.encode()).hexdigest()
    cached = cache_get(key)
    if cached: return cached

    tracks = []

    # 1. YouTube Data API (most reliable for search)
    if YT_API_KEY and not tracks:
        try:
            r = requests.get('https://www.googleapis.com/youtube/v3/search', params={
                'part': 'snippet', 'q': query, 'type': 'video',
                'videoCategoryId': '10', 'maxResults': limit, 'key': YT_API_KEY,
            }, timeout=10)
            items = r.json().get('items', [])
            ids = ','.join([i['id']['videoId'] for i in items if i.get('id', {}).get('videoId')])
            dur_map = {}
            if ids:
                vr = requests.get('https://www.googleapis.com/youtube/v3/videos', params={
                    'part': 'contentDetails', 'id': ids, 'key': YT_API_KEY,
                }, timeout=10)
                for v in vr.json().get('items', []):
                    m = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?',
                                 v.get('contentDetails', {}).get('duration', ''))
                    if m:
                        h, mn, s = (int(x or 0) for x in m.groups())
                        total = h*3600 + mn*60 + s
                        mm, ss = divmod(total, 60)
                        dur_map[v['id']] = f'{mm}:{ss:02d}'
            for item in items:
                vid = item.get('id', {}).get('videoId', '')
                if not vid: continue
                sn = item.get('snippet', {})
                thumb = sn.get('thumbnails', {}).get('high', {}).get('url') or \
                        f'https://img.youtube.com/vi/{vid}/mqdefault.jpg'
                tracks.append({
                    'videoId': vid, 'title': sn.get('title', 'Unknown'),
                    'artist': sn.get('channelTitle', 'Unknown').replace(' - Topic', ''),
                    'thumbnail': thumb, 'duration': dur_map.get(vid, ''),
                    'url': f'https://youtube.com/watch?v={vid}',
                })
        except Exception as e:
            log.error(f'YouTube API search error: {e}')

    # 2. Piped fallback
    if not tracks:
        random.shuffle(PIPED_INSTANCES)
        for host in PIPED_INSTANCES:
            try:
                r = requests.get(f'{host}/search', params={'q': query, 'filter': 'music_songs'}, timeout=8)
                items = r.json().get('items', [])
                for item in items[:limit]:
                    vid = item.get('url', '').replace('/watch?v=', '')
                    if not vid: continue
                    dur = item.get('duration', 0) or 0
                    mm, ss = divmod(int(dur), 60)
                    tracks.append({
                        'videoId': vid, 'title': item.get('title', 'Unknown'),
                        'artist': item.get('uploaderName', 'Unknown').replace(' - Topic', ''),
                        'thumbnail': item.get('thumbnail', f'https://img.youtube.com/vi/{vid}/mqdefault.jpg'),
                        'duration': f'{mm}:{ss:02d}' if dur else '',
                        'url': f'https://youtube.com/watch?v={vid}',
                    })
                if tracks: break
            except Exception as e:
                log.warning(f'Piped {host} failed: {e}')

    # 3. Invidious fallback
    if not tracks:
        random.shuffle(INVIDIOUS_INSTANCES)
        for host in INVIDIOUS_INSTANCES:
            try:
                r = requests.get(f'{host}/api/v1/search', params={'q': query, 'type': 'video'}, timeout=8)
                items = r.json() if isinstance(r.json(), list) else []
                for item in items[:limit]:
                    vid = item.get('videoId', '')
                    if not vid: continue
                    dur = item.get('lengthSeconds', 0) or 0
                    mm, ss = divmod(int(dur), 60)
                    thumb = f'https://img.youtube.com/vi/{vid}/mqdefault.jpg'
                    tracks.append({
                        'videoId': vid, 'title': item.get('title', 'Unknown'),
                        'artist': item.get('author', 'Unknown').replace(' - Topic', ''),
                        'thumbnail': thumb, 'duration': f'{mm}:{ss:02d}' if dur else '',
                        'url': f'https://youtube.com/watch?v={vid}',
                    })
                if tracks: break
            except Exception as e:
                log.warning(f'Invidious {host} failed: {e}')

    if tracks: cache_set(key, tracks)
    return tracks

# ── STREAM ────────────────────────────────────────────────────────────────────
def get_stream_url(video_id):
    key = f'stream:{video_id}'
    cached = cache_get(key)
    if cached: return cached

    # 1. Try Piped instances
    random.shuffle(PIPED_INSTANCES)
    for host in PIPED_INSTANCES:
        try:
            r = requests.get(f'{host}/streams/{video_id}', timeout=10)
            data = r.json()
            streams = data.get('audioStreams', [])
            if streams:
                # Pick best quality m4a or any audio
                m4a = [s for s in streams if 'm4a' in s.get('mimeType','') or 'mp4' in s.get('mimeType','')]
                best = (m4a or streams)[0]
                url = best.get('url', '')
                if url:
                    cache_set(key, url, mins=4)
                    return url
        except Exception as e:
            log.warning(f'Piped stream {host}: {e}')

    # 2. Try Invidious instances
    random.shuffle(INVIDIOUS_INSTANCES)
    for host in INVIDIOUS_INSTANCES:
        try:
            r = requests.get(f'{host}/api/v1/videos/{video_id}', timeout=10)
            data = r.json()
            streams = data.get('adaptiveFormats', [])
            audio = [s for s in streams if s.get('type','').startswith('audio')]
            if audio:
                best = sorted(audio, key=lambda x: x.get('bitrate', 0), reverse=True)[0]
                url = best.get('url', '')
                if url:
                    cache_set(key, url, mins=4)
                    return url
        except Exception as e:
            log.warning(f'Invidious stream {host}: {e}')

    # 3. Cobalt.tools
    try:
        r = requests.post('https://api.cobalt.tools/', 
            headers={'Content-Type': 'application/json', 'Accept': 'application/json'},
            json={'url': f'https://youtube.com/watch?v={video_id}', 'aFormat': 'mp3', 'isAudioOnly': True},
            timeout=15)
        data = r.json()
        url = data.get('url', '')
        if url:
            cache_set(key, url, mins=4)
            return url
    except Exception as e:
        log.warning(f'Cobalt failed: {e}')

    return ''

# ── ROUTES ────────────────────────────────────────────────────────────────────
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
        if not url:
            return jsonify({'error': 'Could not get stream URL'}), 404
        return jsonify({'url': url, 'videoId': video_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/proxy/<video_id>')
def proxy(video_id):
    try:
        url = get_stream_url(video_id)
        if not url: return jsonify({'error': 'No URL'}), 404
        headers = {'User-Agent': 'Mozilla/5.0', 'Range': request.headers.get('Range', 'bytes=0-')}
        r = requests.get(url, headers=headers, stream=True, timeout=15)
        def generate():
            for chunk in r.iter_content(8192):
                if chunk: yield chunk
        resp_headers = {'Content-Type': r.headers.get('Content-Type', 'audio/mp4'), 'Accept-Ranges': 'bytes'}
        if 'Content-Length' in r.headers: resp_headers['Content-Length'] = r.headers['Content-Length']
        if 'Content-Range' in r.headers: resp_headers['Content-Range'] = r.headers['Content-Range']
        return Response(stream_with_context(generate()), status=r.status_code, headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<video_id>')
def download(video_id):
    title = request.args.get('title', video_id)
    safe = re.sub(r'[^\w\s-]', '', title)[:50]
    out = CACHE_DIR / f'{video_id}.mp3'
    if not out.exists():
        # Try yt-dlp first
        try:
            subprocess.run([
                'yt-dlp', f'https://youtube.com/watch?v={video_id}',
                '-x', '--audio-format', 'mp3', '--audio-quality', '0',
                '-o', str(CACHE_DIR / f'{video_id}.%(ext)s'),
                '--no-warnings', '--quiet'
            ], timeout=120)
        except: pass
        # Fallback - stream URL and save
        if not out.exists():
            try:
                url = get_stream_url(video_id)
                if url:
                    r = requests.get(url, stream=True, timeout=60)
                    with open(str(out), 'wb') as f:
                        for chunk in r.iter_content(8192):
                            if chunk: f.write(chunk)
            except: pass
    if out.exists():
        return send_file(str(out), as_attachment=True, download_name=f'{safe}.mp3', mimetype='audio/mpeg')
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
