"""
Sona Backend — Flask + Paxsenix API + yt-dlp fallback
"""
import os, json, time, threading, logging, re, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import requests
import sqlite3

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('sona')

app = Flask(__name__)
CORS(app, origins=['*'])

CACHE_DIR = Path(os.getenv('CACHE_DIR', '/tmp/sona_cache'))
DB_PATH = os.getenv('DB_PATH', 'sona.db')
PORT = int(os.getenv('PORT', 5000))
PAXSENIX = 'https://api.paxsenix.biz.id'

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ── DB ───────────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS search_cache (
            key TEXT PRIMARY KEY, data TEXT, expires_at TEXT
        );
        CREATE TABLE IF NOT EXISTS notified_tracks (
            video_id TEXT PRIMARY KEY, title TEXT, notified_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

# ── SEARCH ───────────────────────────────────────────────────────────────────
def search_tracks(query: str, limit: int = 10) -> list:
    cache_key = hashlib.md5(f'{query}:{limit}'.encode()).hexdigest()
    conn = get_db()
    row = conn.execute('SELECT data, expires_at FROM search_cache WHERE key=?', (cache_key,)).fetchone()
    if row:
        try:
            if datetime.fromisoformat(row['expires_at']) > datetime.now():
                conn.close()
                return json.loads(row['data'])
        except: pass
    conn.close()

    tracks = []
    # Try paxsenix first
    try:
        r = requests.get(f'{PAXSENIX}/yt/search', params={'q': query, 'max': limit}, timeout=15)
        data = r.json()
        items = data.get('data') or data.get('results') or data.get('items') or []
        for item in items[:limit]:
            vid = item.get('id') or item.get('videoId') or item.get('video_id','')
            tracks.append({
                'videoId': vid,
                'title': item.get('title','Unknown'),
                'artist': item.get('channel') or item.get('uploader','Unknown'),
                'thumbnail': item.get('thumbnail') or item.get('thumb') or (f'https://img.youtube.com/vi/{vid}/mqdefault.jpg' if vid else ''),
                'duration': item.get('duration',''),
                'url': f'https://youtube.com/watch?v={vid}' if vid else '',
            })
    except Exception as e:
        log.error(f'Paxsenix search error: {e}')

    # Fallback to yt-dlp if paxsenix returned nothing
    if not tracks:
        try:
            import yt_dlp
            opts = {'quiet':True,'no_warnings':True,'extract_flat':True,'skip_download':True,'playlistend':limit}
            with yt_dlp.YoutubeDL(opts) as ydl:
                result = ydl.extract_info(f'ytsearch{limit}:{query}', download=False)
                for e in (result.get('entries') or []):
                    if not e: continue
                    vid = e.get('id','')
                    dur = e.get('duration',0) or 0
                    m,s = divmod(int(dur),60)
                    tracks.append({
                        'videoId': vid,
                        'title': e.get('title','Unknown'),
                        'artist': e.get('uploader','Unknown').replace(' - Topic',''),
                        'thumbnail': e.get('thumbnail') or f'https://img.youtube.com/vi/{vid}/mqdefault.jpg',
                        'duration': f'{m}:{s:02d}' if dur else '',
                        'url': f'https://youtube.com/watch?v={vid}',
                    })
        except Exception as e:
            log.error(f'yt-dlp search error: {e}')

    if tracks:
        conn = get_db()
        expires = (datetime.now() + timedelta(minutes=30)).isoformat()
        conn.execute('INSERT OR REPLACE INTO search_cache VALUES (?,?,?)', (cache_key, json.dumps(tracks), expires))
        conn.commit()
        conn.close()
    return tracks

# ── STREAM ───────────────────────────────────────────────────────────────────
def get_stream_url(video_id: str) -> str:
    url = f'https://youtube.com/watch?v={video_id}'
    # Try paxsenix
    try:
        r = requests.get(f'{PAXSENIX}/dl/ytmp3', params={'url': url}, timeout=20)
        data = r.json()
        stream = data.get('url') or data.get('download_url') or data.get('link')
        if stream:
            return stream
    except Exception as e:
        log.error(f'Paxsenix stream error: {e}')
    # Fallback yt-dlp
    try:
        import yt_dlp
        opts = {'quiet':True,'no_warnings':True,'skip_download':True,'format':'bestaudio/best'}
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
            formats = info.get('formats',[])
            audio = [f for f in formats if f.get('acodec')!='none' and f.get('vcodec')=='none']
            if audio:
                return sorted(audio, key=lambda x: x.get('abr',0) or 0, reverse=True)[0]['url']
            return info.get('url','')
    except Exception as e:
        log.error(f'yt-dlp stream error: {e}')
    return ''

# ── ROUTES ───────────────────────────────────────────────────────────────────
@app.route('/api/health')
def health():
    return jsonify({'status':'ok','service':'Sona','version':'2.0'})

@app.route('/api/search')
def search():
    q = request.args.get('q','').strip()
    limit = min(int(request.args.get('limit',10)), 20)
    if not q:
        return jsonify({'tracks':[],'error':'Query required'}), 400
    tracks = search_tracks(q, limit)
    return jsonify({'tracks':tracks,'query':q,'count':len(tracks)})

@app.route('/api/stream/<video_id>')
def stream(video_id):
    try:
        url = get_stream_url(video_id)
        if not url:
            return jsonify({'error':'Could not get stream URL'}), 404
        return jsonify({'url':url,'videoId':video_id})
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/download/<video_id>')
def download(video_id):
    title = request.args.get('title', video_id)
    url = f'https://youtube.com/watch?v={video_id}'
    try:
        r = requests.get(f'{PAXSENIX}/dl/ytmp3', params={'url':url}, timeout=20)
        data = r.json()
        dl_url = data.get('url') or data.get('download_url') or data.get('link')
        if dl_url:
            return jsonify({'url': dl_url, 'title': title})
        return jsonify({'error':'Download failed'}), 500
    except Exception as e:
        return jsonify({'error':str(e)}), 500

@app.route('/api/artists')
def artists():
    q = request.args.get('q','')
    tracks = search_tracks(f'{q} artist', 5)
    names = list(dict.fromkeys([t['artist'] for t in tracks if t['artist']]))
    return jsonify({'artists': names})

@app.route('/api/subscribe', methods=['POST'])
def subscribe():
    return jsonify({'success': True})

# ── SERVE FRONTEND ────────────────────────────────────────────────────────────
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def serve(path):
    frontend = Path('frontend')
    f = frontend / path
    if path and f.exists() and f.is_file():
        return send_file(str(f))
    return send_file(str(frontend / 'index.html'))

if __name__ == '__main__':
    init_db()
    log.info(f'🎵 Sona starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
