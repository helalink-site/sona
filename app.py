"""
Sona Backend — Flask + yt-dlp
Handles: search, stream, download, push subscriptions, new release monitoring
"""

import os, json, time, threading, logging, re, hashlib
from pathlib import Path
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, send_file, Response
from flask_cors import CORS
import yt_dlp
import sqlite3
import requests
from pywebpush import webpush, WebPushException
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.backends import default_backend
import base64

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger('sona')

app = Flask(__name__)
CORS(app, origins=['*'])

# ─── CONFIG ─────────────────────────────────────────────────────────────────
CACHE_DIR = Path(os.getenv('CACHE_DIR', '/tmp/sona_cache'))
DB_PATH   = os.getenv('DB_PATH', 'sona.db')
VAPID_PRIVATE_KEY = os.getenv('VAPID_PRIVATE_KEY', '')
VAPID_PUBLIC_KEY  = os.getenv('VAPID_PUBLIC_KEY', '')
VAPID_EMAIL       = os.getenv('VAPID_EMAIL', 'mailto:admin@sona.app')
PORT = int(os.getenv('PORT', 5000))

CACHE_DIR.mkdir(parents=True, exist_ok=True)

# ─── DATABASE ────────────────────────────────────────────────────────────────
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY,
            endpoint TEXT UNIQUE NOT NULL,
            p256dh TEXT,
            auth TEXT,
            prefs TEXT DEFAULT '{}',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS monitored_channels (
            id TEXT PRIMARY KEY,
            channel_id TEXT UNIQUE NOT NULL,
            channel_name TEXT,
            last_video_id TEXT,
            last_checked TEXT
        );
        CREATE TABLE IF NOT EXISTS notified_tracks (
            video_id TEXT PRIMARY KEY,
            title TEXT,
            notified_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS search_cache (
            key TEXT PRIMARY KEY,
            data TEXT,
            expires_at TEXT
        );
    """)
    conn.commit()
    conn.close()
    log.info('✅ Database initialized')

# ─── YT-DLP HELPERS ──────────────────────────────────────────────────────────
YDL_OPTS_SEARCH = {
    'quiet': True,
    'no_warnings': True,
    'extract_flat': True,
    'skip_download': True,
    'default_search': 'ytsearch',
}

YDL_OPTS_INFO = {
    'quiet': True,
    'no_warnings': True,
    'skip_download': True,
    'format': 'bestaudio/best',
}

def search_youtube(query: str, limit: int = 10) -> list:
    """Search YouTube and return track list."""
    cache_key = hashlib.md5(f'search:{query}:{limit}'.encode()).hexdigest()

    # Check cache
    conn = get_db()
    row = conn.execute('SELECT data, expires_at FROM search_cache WHERE key=?', (cache_key,)).fetchone()
    if row and datetime.fromisoformat(row['expires_at']) > datetime.now():
        conn.close()
        return json.loads(row['data'])
    conn.close()

    opts = {**YDL_OPTS_SEARCH, 'playlistend': limit}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            result = ydl.extract_info(f'ytsearch{limit}:{query}', download=False)
            entries = result.get('entries', []) if result else []
            tracks = [format_track(e) for e in entries if e]
        except Exception as e:
            log.error(f'Search error: {e}')
            return []

    # Cache for 30 minutes
    conn = get_db()
    expires = (datetime.now() + timedelta(minutes=30)).isoformat()
    conn.execute('INSERT OR REPLACE INTO search_cache VALUES (?,?,?)', (cache_key, json.dumps(tracks), expires))
    conn.commit()
    conn.close()
    return tracks

def format_track(entry: dict) -> dict:
    video_id = entry.get('id', '')
    duration_s = entry.get('duration', 0) or 0
    m, s = divmod(int(duration_s), 60)
    return {
        'videoId': video_id,
        'title': entry.get('title', 'Unknown'),
        'artist': entry.get('uploader', entry.get('channel', 'Unknown')).replace(' - Topic', ''),
        'thumbnail': entry.get('thumbnail') or f'https://img.youtube.com/vi/{video_id}/mqdefault.jpg',
        'duration': f'{m}:{s:02d}' if duration_s else '',
        'url': f'https://youtube.com/watch?v={video_id}',
    }

def get_stream_url(video_id: str) -> str:
    """Get direct audio stream URL."""
    url = f'https://youtube.com/watch?v={video_id}'
    opts = {
        **YDL_OPTS_INFO,
        'format': 'bestaudio[ext=m4a]/bestaudio/best',
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
        formats = info.get('formats', [])
        # Pick best audio-only format
        audio_formats = [f for f in formats if f.get('acodec') != 'none' and f.get('vcodec') == 'none']
        if audio_formats:
            best = sorted(audio_formats, key=lambda x: x.get('abr', 0) or 0, reverse=True)[0]
            return best['url']
        return info.get('url', '')

def download_audio(video_id: str, title: str) -> Path:
    """Download audio to cache and return file path."""
    safe_title = re.sub(r'[^\w\s-]', '', title)[:50]
    out_path = CACHE_DIR / f'{video_id}.mp3'
    if out_path.exists():
        return out_path

    url = f'https://youtube.com/watch?v={video_id}'
    opts = {
        'quiet': True,
        'no_warnings': True,
        'format': 'bestaudio/best',
        'outtmpl': str(CACHE_DIR / f'{video_id}.%(ext)s'),
        'postprocessors': [{
            'key': 'FFmpegExtractAudio',
            'preferredcodec': 'mp3',
            'preferredquality': '192',
        }],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    return out_path

# ─── ROUTES ──────────────────────────────────────────────────────────────────

@app.route('/api/health')
def health():
    return jsonify({'status': 'ok', 'service': 'Sona', 'version': '1.0.0'})

@app.route('/api/search')
def search():
    q = request.args.get('q', '').strip()
    limit = min(int(request.args.get('limit', 10)), 20)
    if not q:
        return jsonify({'tracks': [], 'error': 'Query required'}), 400
    tracks = search_youtube(q, limit)
    return jsonify({'tracks': tracks, 'query': q, 'count': len(tracks)})

@app.route('/api/stream/<video_id>')
def stream(video_id):
    """Return direct audio stream URL (not proxied, browser streams directly)."""
    try:
        url = get_stream_url(video_id)
        if not url:
            return jsonify({'error': 'Could not get stream URL'}), 404
        return jsonify({'url': url, 'videoId': video_id})
    except Exception as e:
        log.error(f'Stream error {video_id}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/download/<video_id>')
def download(video_id):
    """Download audio file and return download URL."""
    title = request.args.get('title', video_id)
    try:
        file_path = download_audio(video_id, title)
        if file_path.exists():
            return send_file(str(file_path), as_attachment=True, download_name=f'{title}.mp3', mimetype='audio/mpeg')
        return jsonify({'error': 'Download failed'}), 500
    except Exception as e:
        log.error(f'Download error {video_id}: {e}')
        return jsonify({'error': str(e)}), 500

@app.route('/api/artists')
def artists():
    q = request.args.get('q', '')
    # Search YouTube for artist channels
    results = search_youtube(f'{q} artist music', 5)
    artist_names = list(dict.fromkeys([t['artist'] for t in results if t['artist']]))
    return jsonify({'artists': artist_names})

@app.route('/api/subscribe', methods=['POST'])
def subscribe():
    """Save push subscription + user prefs."""
    data = request.get_json()
    sub = data.get('subscription', {})
    prefs = data.get('prefs', {})
    if not sub.get('endpoint'):
        return jsonify({'error': 'No endpoint'}), 400

    sub_id = hashlib.md5(sub['endpoint'].encode()).hexdigest()
    conn = get_db()
    conn.execute(
        'INSERT OR REPLACE INTO subscriptions VALUES (?,?,?,?,?,?)',
        (sub_id, sub['endpoint'],
         sub.get('keys', {}).get('p256dh', ''),
         sub.get('keys', {}).get('auth', ''),
         json.dumps(prefs),
         datetime.now().isoformat())
    )

    # Register followed artists as monitored channels
    artists = prefs.get('artists', [])
    for artist in artists:
        register_artist_monitor(artist, conn)

    conn.commit()
    conn.close()
    log.info(f'New subscription: {sub_id}')
    return jsonify({'success': True, 'id': sub_id})

@app.route('/api/unsubscribe', methods=['POST'])
def unsubscribe():
    data = request.get_json()
    endpoint = data.get('endpoint', '')
    conn = get_db()
    conn.execute('DELETE FROM subscriptions WHERE endpoint=?', (endpoint,))
    conn.commit()
    conn.close()
    return jsonify({'success': True})

# ─── MONITORING ───────────────────────────────────────────────────────────────
def register_artist_monitor(artist_name: str, conn=None):
    """Search for artist's YouTube channel and register for monitoring."""
    close = conn is None
    if conn is None:
        conn = get_db()
    try:
        tracks = search_youtube(f'{artist_name} official music video', 1)
        if tracks:
            channel_id = hashlib.md5(artist_name.encode()).hexdigest()[:16]
            conn.execute(
                'INSERT OR IGNORE INTO monitored_channels VALUES (?,?,?,?,?)',
                (channel_id, artist_name, artist_name, None, None)
            )
    except Exception as e:
        log.error(f'Register monitor error: {e}')
    finally:
        if close:
            conn.commit()
            conn.close()

def check_new_releases():
    """Background thread: check monitored channels for new releases every hour."""
    while True:
        try:
            conn = get_db()
            channels = conn.execute('SELECT * FROM monitored_channels').fetchall()
            subs = conn.execute('SELECT * FROM subscriptions').fetchall()

            for channel in channels:
                artist = channel['channel_name']
                log.info(f'Checking new releases for: {artist}')

                tracks = search_youtube(f'{artist} new 2025', 3)
                if not tracks:
                    continue

                for track in tracks:
                    vid = track['videoId']
                    already = conn.execute('SELECT 1 FROM notified_tracks WHERE video_id=?', (vid,)).fetchone()
                    if already:
                        continue

                    # This is a new track — notify all subscribers who follow this artist
                    for sub in subs:
                        sub_prefs = json.loads(sub['prefs'] or '{}')
                        followed = sub_prefs.get('artists', [])
                        if artist in followed:
                            send_push(sub, {
                                'title': f'🎵 New from {artist}',
                                'body': track['title'],
                                'url': f'/?play={vid}',
                                'icon': track['thumbnail'],
                            })

                    # Mark as notified
                    conn.execute('INSERT OR IGNORE INTO notified_tracks VALUES (?,?,?)',
                                 (vid, track['title'], datetime.now().isoformat()))

                # Update last checked
                conn.execute('UPDATE monitored_channels SET last_checked=? WHERE channel_name=?',
                             (datetime.now().isoformat(), artist))

            conn.commit()
            conn.close()
        except Exception as e:
            log.error(f'Monitor error: {e}')

        time.sleep(3600)  # Check every hour

def send_push(sub: dict, payload: dict):
    """Send web push notification to a subscriber."""
    if not VAPID_PRIVATE_KEY or not VAPID_PUBLIC_KEY:
        log.warning('VAPID keys not configured — skipping push')
        return
    try:
        webpush(
            subscription_info={
                'endpoint': sub['endpoint'],
                'keys': {'p256dh': sub['p256dh'], 'auth': sub['auth']}
            },
            data=json.dumps(payload),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={'sub': VAPID_EMAIL}
        )
        log.info(f'Push sent to {sub["id"][:8]}...')
    except WebPushException as e:
        if e.response and e.response.status_code == 410:
            # Subscription expired — remove it
            conn = get_db()
            conn.execute('DELETE FROM subscriptions WHERE endpoint=?', (sub['endpoint'],))
            conn.commit()
            conn.close()

# ─── SERVE FRONTEND ───────────────────────────────────────────────────────────
@app.route('/')
@app.route('/<path:path>')
def serve_frontend(path='index.html'):
    frontend = Path('frontend')
    file = frontend / path
    if file.exists() and file.is_file():
        return send_file(str(file))
    return send_file(str(frontend / 'index.html'))

# ─── STARTUP ─────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    init_db()
    # Start background monitor thread
    monitor_thread = threading.Thread(target=check_new_releases, daemon=True)
    monitor_thread.start()
    log.info(f'🎵 Sona backend starting on port {PORT}')
    app.run(host='0.0.0.0', port=PORT, debug=False)
