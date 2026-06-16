import os, json, hashlib, re, subprocess, logging, random, time
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
SC_CLIENT_ID = os.getenv('SC_CLIENT_ID', '')
CACHE_DIR.mkdir(parents=True, exist_ok=True)
_cache = {}
_sc_client_id = SC_CLIENT_ID or None

def cache_get(key):
    if key in _cache:
        data, exp = _cache[key]
        if datetime.now() < exp: return data
        del _cache[key]
    return None

def cache_set(key, data, mins=30):
    _cache[key] = (data, datetime.now() + timedelta(minutes=mins))

# ── SOUNDCLOUD CLIENT ID ──────────────────────────────────────────────────────
def get_sc_client_id():
    global _sc_client_id
    if _sc_client_id: return _sc_client_id
    try:
        r = requests.get('https://soundcloud.com', timeout=10,
                        headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'})
        # Find JS files
        js_urls = re.findall(r'https://a-v2\.sndcdn\.com/assets/[^"]+\.js', r.text)
        for js_url in js_urls[:5]:
            js = requests.get(js_url, timeout=10).text
            match = re.search(r'client_id:"([a-zA-Z0-9]{32})"', js)
            if match:
                _sc_client_id = match.group(1)
                log.info(f'SoundCloud client_id: {_sc_client_id}')
                return _sc_client_id
    except Exception as e:
        log.error(f'SC client_id error: {e}')
    # Known working fallbacks
    for cid in ['iZIs9mchVcX5lhVRyQGGAYlNPVldzAoX', 'a3e059563d7fd3372b49b37f00a00bcf']:
        try:
            r = requests.get(f'https://api-v2.soundcloud.com/search?q=test&limit=1&client_id={cid}', timeout=5)
            if r.status_code == 200:
                _sc_client_id = cid
                return cid
        except: pass
    return None

# ── SEARCH ────────────────────────────────────────────────────────────────────
def search_soundcloud(query, limit=10):
    cid = get_sc_client_id()
    if not cid: return []
    try:
        r = requests.get('https://api-v2.soundcloud.com/search/tracks', params={
            'q': query, 'limit': limit, 'client_id': cid,
            'filter.duration': 'medium',
        }, timeout=10, headers={'User-Agent': 'Mozilla/5.0'})
        if r.status_code == 401:
            global _sc_client_id
            _sc_client_id = None
            return []
        items = r.json().get('collection', [])
        tracks = []
        for item in items:
            if item.get('policy') == 'BLOCK': continue
            dur = item.get('duration', 0) // 1000
            mm, ss = divmod(dur, 60)
            thumb = item.get('artwork_url', '') or item.get('user', {}).get('avatar_url', '')
            if thumb: thumb = thumb.replace('large', 't500x500')
            tracks.append({
                'videoId': str(item.get('id', '')),
                'title': item.get('title', 'Unknown'),
                'artist': item.get('user', {}).get('username', 'Unknown'),
                'thumbnail': thumb,
                'duration': f'{mm}:{ss:02d}' if dur else '',
                'url': item.get('permalink_url', ''),
                'streamUrl': item.get('stream_url', ''),
                'source': 'soundcloud',
            })
        return tracks
    except Exception as e:
        log.error(f'SC search error: {e}')
        return []

def search_tracks(query, limit=10):
    key = hashlib.md5(f'sc:{query}:{limit}'.encode()).hexdigest()
    cached = cache_get(key)
    if cached: return cached

    # 1. SoundCloud
    tracks = search_soundcloud(query, limit)

    # 2. YouTube API fallback if SC empty
    if not tracks and YT_API_KEY:
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
                        mm2, ss2 = divmod(total, 60)
                        dur_map[v['id']] = f'{mm2}:{ss2:02d}'
            for item in items:
                vid = item.get('id', {}).get('videoId', '')
                if not vid: continue
                sn = item.get('snippet', {})
                tracks.append({
                    'videoId': vid, 'title': sn.get('title', 'Unknown'),
                    'artist': sn.get('channelTitle', 'Unknown').replace(' - Topic', ''),
                    'thumbnail': sn.get('thumbnails', {}).get('high', {}).get('url', f'https://img.youtube.com/vi/{vid}/mqdefault.jpg'),
                    'duration': dur_map.get(vid, ''), 'url': f'https://youtube.com/watch?v={vid}',
                    'source': 'youtube',
                })
        except Exception as e:
            log.error(f'YT search error: {e}')

    if tracks: cache_set(key, tracks)
    return tracks

# ── STREAM ────────────────────────────────────────────────────────────────────
def get_sc_stream_url(track_id):
    cid = get_sc_client_id()
    if not cid: return ''
    try:
        # Get track info
        r = requests.get(f'https://api-v2.soundcloud.com/tracks/{track_id}',
                        params={'client_id': cid}, timeout=10)
        track = r.json()
        # Get progressive stream URL
        media = track.get('media', {}).get('transcodings', [])
        progressive = [m for m in media if m.get('format', {}).get('protocol') == 'progressive']
        if not progressive:
            progressive = media  # fallback to any
        if not progressive: return ''
        stream_url = progressive[0].get('url', '')
        if not stream_url: return ''
        # Resolve stream URL
        r2 = requests.get(stream_url, params={'client_id': cid}, timeout=10)
        return r2.json().get('url', '')
    except Exception as e:
        log.error(f'SC stream error: {e}')
        return ''

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route('/api/health')
def health():
    cid = get_sc_client_id()
    return jsonify({'status': 'ok', 'service': 'Sona', 'sc_ready': bool(cid)})

@app.route('/api/search')
def search():
    q = request.args.get('q', '').strip()
    limit = min(int(request.args.get('limit', 10)), 20)
    if not q: return jsonify({'tracks': [], 'error': 'Query required'}), 400
    tracks = search_tracks(q, limit)
    return jsonify({'tracks': tracks, 'query': q, 'count': len(tracks)})

@app.route('/api/stream/<track_id>')
def stream(track_id):
    try:
        url = get_sc_stream_url(track_id)
        if not url:
            return jsonify({'error': 'Could not get stream URL'}), 404
        return jsonify({'url': url, 'trackId': track_id})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/proxy/<track_id>')
def proxy(track_id):
    try:
        url = get_sc_stream_url(track_id)
        if not url:
            return jsonify({'error': 'No stream URL'}), 404
        range_header = request.headers.get('Range')
        headers = {'User-Agent': 'Mozilla/5.0'}
        if range_header: headers['Range'] = range_header
        r = requests.get(url, headers=headers, stream=True, timeout=30)
        def generate():
            for chunk in r.iter_content(chunk_size=16384):
                if chunk: yield chunk
        resp_headers = {
            'Content-Type': r.headers.get('Content-Type', 'audio/mpeg'),
            'Accept-Ranges': 'bytes',
            'Access-Control-Allow-Origin': '*',
        }
        if 'Content-Length' in r.headers: resp_headers['Content-Length'] = r.headers['Content-Length']
        if 'Content-Range' in r.headers: resp_headers['Content-Range'] = r.headers['Content-Range']
        return Response(stream_with_context(generate()),
                       status=r.status_code if r.status_code in [200,206] else 200,
                       headers=resp_headers)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<track_id>')
def download(track_id):
    title = request.args.get('title', track_id)
    safe = re.sub(r'[^\w\s-]', '', title)[:50]
    out = CACHE_DIR / f'{track_id}.mp3'
    if not out.exists():
        url = get_sc_stream_url(track_id)
        if url:
            try:
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
    tracks = search_tracks(f'{q}', 5)
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
    # Pre-fetch SC client ID on startup
    cid = get_sc_client_id()
    log.info(f'Sona starting - SC client_id: {"OK" if cid else "FAILED"}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
