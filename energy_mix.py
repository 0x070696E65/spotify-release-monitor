"""Resolve and append the songs in the manually managed My Energy Mix playlist."""

import argparse
from dataclasses import asdict, dataclass
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import tempfile
import time

from release_monitor import ENV_PATH, PLAYLIST_BATCH_SIZE, normalize_title
from spotify_api import ApiError, MonitorError, Settings, Spotify, run_lock


TRACKS_PATH = Path(__file__).resolve().with_name("energy_mix_tracks.json")
CACHE_FILENAME = ".energy-mix-cache.json"
CACHE_TTL = 48 * 60 * 60
PLAYLIST_NAME = "My Energy Mix"
PLAYLIST_DESCRIPTION = "My Energy Mix - managed by Spotify Release Monitor (manual)."
PLAYLIST_ID_KEY = "SPOTIFY_ENERGY_MIX_PLAYLIST_ID"
SPOTIFY_ID = re.compile(r"[A-Za-z0-9]+")
TRACK_URI = re.compile(r"spotify:track:([A-Za-z0-9]+)")
VERSION_MARKER = re.compile(
    r"\b(?:live|remix(?:es)?|karaoke|instrumental|off vocal|acoustic version|sped up|slowed|nightcore)\b"
    r"|ライブ|リミックス|カラオケ|インスト|弾き語り"
)
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Song:
    artist: str
    title: str
    spotify_artist: str = ""
    spotify_titles: tuple[str, ...] = ()
    album_id: str = ""
    track_id: str = ""


def load_songs(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise MonitorError(f"{path.name} を読み込めません。ファイルの場所と UTF-8 の JSON を確認してください。") from None
    rows = data.get("tracks") if isinstance(data, dict) else None
    if not isinstance(rows, list) or not rows:
        raise MonitorError(f"{path.name} の tracks に1曲以上を設定してください。")
    songs = []
    for index, row in enumerate(rows, 1):
        message = f"{path.name} の tracks {index} 行目の設定が不正です。"
        if not isinstance(row, dict) or set(row) - Song.__dataclass_fields__.keys():
            raise MonitorError(message)
        for field in ("artist", "title", "spotify_artist", "album_id", "track_id"):
            if field in row or field in ("artist", "title"):
                if not isinstance(row.get(field), str) or not row[field].strip():
                    raise MonitorError(message)
        for field in ("album_id", "track_id"):
            if field in row and not SPOTIFY_ID.fullmatch(row[field]):
                raise MonitorError(message)
        titles = row.get("spotify_titles", [])
        if not isinstance(titles, list) or any(not isinstance(title, str) or not title.strip() for title in titles):
            raise MonitorError(message)
        songs.append(Song(**{**row, "spotify_titles": tuple(titles)}))
    return songs


def normalized(value):
    return normalize_title(value.replace("’", "'"))


def search_query(title, artist):
    # Quotes and backslashes in names are checked during exact matching instead.
    title = title.replace('"', " ").replace("\\", " ")
    artist = artist.replace('"', " ").replace("\\", " ")
    return f'track:"{title}" artist:"{artist}"'


def track_matches(song, track):
    if not isinstance(track, dict):
        return False
    uri = track.get("uri")
    if (not isinstance(uri, str) or not TRACK_URI.fullmatch(uri)
            or track.get("is_local") or track.get("is_playable") is False
            or track.get("restrictions")):
        return False
    credits = track.get("artists")
    album = track.get("album")
    if (not isinstance(credits, list) or not credits or not isinstance(album, dict)
            or any(not isinstance(artist, dict) or not isinstance(artist.get("name"), str) for artist in credits)
            or not isinstance(track.get("name"), str) or not isinstance(album.get("name"), str)):
        return False
    titles = {normalized(title) for title in (song.title, *song.spotify_titles)}
    artists = {normalized(artist) for artist in (song.artist, song.spotify_artist) if artist}
    album_name = normalized(album["name"])
    return (normalized(track["name"]) in titles and normalized(credits[0]["name"]) in artists
            and (not song.album_id or song.album_id == album.get("id"))
            and (not song.track_id or uri == f"spotify:track:{song.track_id}")
            and (album_name in titles or not VERSION_MARKER.search(album_name)))


def resolve_song(spotify, song):
    if song.track_id:
        try:
            track = spotify.call("GET", f"/tracks/{song.track_id}")
        except ApiError as error:
            if error.status != 404:
                raise
            return None, []
        return (track, []) if track_matches(song, track) else (None, [])

    # One search page in the common case. Search a verified alias only if needed.
    preferred = song.spotify_titles[0] if song.spotify_titles else song.title
    fallback = song.spotify_titles[1] if len(song.spotify_titles) > 1 else song.title
    artist = song.spotify_artist or song.artist
    queries = dict.fromkeys((search_query(preferred, artist), search_query(fallback, song.artist)))
    for query in queries:
        page = spotify.call("GET", "/search", params={"q": query, "type": "track", "limit": 10})
        items = page["tracks"]["items"]
        if not isinstance(items, list):
            raise MonitorError("Spotify の曲検索結果の形式が不正です。")
        matches = {track["uri"]: track for track in items if track_matches(song, track)}
        if not matches:
            continue
        candidates = list(matches.values())
        recordings = {((track.get("external_ids") or {}).get("isrc"), track["artists"][0].get("id"))
                      for track in candidates}
        if len(candidates) == 1 or (len(recordings) == 1 and all(next(iter(recordings)))):
            return candidates[0], []
        return None, candidates
    return None, []


def cache_scope(spotify):
    settings = spotify.settings
    credentials = f"{settings.get('SPOTIFY_CLIENT_ID')}\0{settings.get('SPOTIFY_REFRESH_TOKEN')}"
    return hashlib.sha256(credentials.encode("utf-8")).hexdigest()


def load_cache(path, scope):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        LOG.warning("検索キャッシュを読み込めませんでした。Spotify で検索します。")
        return {}
    if not isinstance(data, dict) or data.get("scope") != scope or not isinstance(data.get("songs"), dict):
        return {}
    now = time.time()
    return {key: entry for key, entry in data["songs"].items()
            if isinstance(entry, dict) and isinstance(entry.get("expires_at"), (int, float))
            and now < entry["expires_at"] <= now + CACHE_TTL
            and all(isinstance(entry.get(field), str) for field in ("uri", "title", "artist", "album"))
            and TRACK_URI.fullmatch(entry["uri"])}


def save_cache(path, scope, songs):
    try:
        temporary = tempfile.NamedTemporaryFile(dir=path.parent, prefix=f"{path.name}.", mode="w",
                                                encoding="utf-8", delete=False)
        try:
            with temporary:
                json.dump({"scope": scope, "songs": songs}, temporary, ensure_ascii=False)
            os.replace(temporary.name, path)
        finally:
            Path(temporary.name).unlink(missing_ok=True)
    except OSError:
        LOG.warning("検索キャッシュを保存できませんでした。")


def resolve_songs(spotify, songs, *, dry_run):
    path = spotify.settings.path.with_name(CACHE_FILENAME)
    scope = cache_scope(spotify)
    cache = load_cache(path, scope)
    changed = False
    selected = {}
    unresolved = 0
    try:
        for index, song in enumerate(songs, 1):
            key = json.dumps(asdict(song), sort_keys=True, ensure_ascii=False)
            entry = cache.get(key)
            if entry is None:
                track, candidates = resolve_song(spotify, song)
                if track is None:
                    unresolved += 1
                    LOG.warning("%02d. %s / %s — %s", index, song.artist, song.title,
                                "候補が複数あります" if candidates else "一致する曲が見つかりません")
                    for candidate in candidates:
                        LOG.warning("  %s / %s", candidate["album"]["name"], track_url(candidate["uri"]))
                    continue
                entry = {"uri": track["uri"], "title": track["name"], "album": track["album"]["name"],
                         "artist": ", ".join(artist["name"] for artist in track["artists"]),
                         "expires_at": time.time() + CACHE_TTL}
                cache[key] = entry
                changed = True
            uri = entry["uri"]
            duplicate = uri in selected
            selected.setdefault(uri, entry)
            if dry_run:
                label = "（重複）" if duplicate else ""
                print(f"{index:02d}. {entry['artist']} / {entry['title']} / {entry['album']} {track_url(uri)}{label}")
    finally:
        current_scope = cache_scope(spotify)
        if changed or (cache and current_scope != scope):
            save_cache(path, current_scope, cache)

    LOG.info("候補 %s 曲、採用 %s 曲、未確定 %s 曲、URI 重複 %s 曲", len(songs), len(selected), unresolved,
             len(songs) - len(selected) - unresolved)
    if unresolved:
        raise MonitorError("未確定の曲があります。候補を確認して energy_mix_tracks.json を修正してください。")
    return list(selected)


def track_url(uri):
    return f"https://open.spotify.com/track/{uri.rsplit(':', 1)[-1]}"


def validate_playlist(playlist, owner, protected_id):
    identifier = playlist["id"]
    if (not SPOTIFY_ID.fullmatch(identifier) or identifier == protected_id or playlist["owner"]["id"] != owner
            or playlist["name"] != PLAYLIST_NAME or playlist.get("public") is not False
            or playlist.get("description") != PLAYLIST_DESCRIPTION):
        raise MonitorError("安全に My Energy Mix と特定できないため、プレイリストを変更していません。")
    return identifier


def find_playlist(spotify):
    owner = spotify.call("GET", "/me")["id"]
    saved_id = spotify.settings.get(PLAYLIST_ID_KEY)
    protected_id = spotify.settings.get("SPOTIFY_PLAYLIST_ID")
    if saved_id:
        if not SPOTIFY_ID.fullmatch(saved_id) or saved_id == protected_id:
            raise MonitorError(f".env の {PLAYLIST_ID_KEY} が不正か、自動更新用と同じ ID です。")
        playlist = spotify.call("GET", f"/playlists/{saved_id}",
                                params={"fields": "id,name,description,public,owner(id)"})
        if validate_playlist(playlist, owner, protected_id) != saved_id:
            raise MonitorError("My Energy Mix の保存済み ID と Spotify の応答が一致しません。")
        return saved_id, False

    existing = [playlist for playlist in spotify.items("/me/playlists", params={"limit": 50})
                if playlist["owner"]["id"] == owner and (
                    playlist["name"] == PLAYLIST_NAME or playlist.get("description") == PLAYLIST_DESCRIPTION)]
    if len(existing) > 1:
        raise MonitorError("同名または同じ管理用説明のプレイリストが複数あります。登録先を確認してください。")
    if existing:
        identifier = validate_playlist(existing[0], owner, protected_id)
    else:
        playlist = spotify.call("POST", "/me/playlists", body={
            "name": PLAYLIST_NAME, "description": PLAYLIST_DESCRIPTION, "public": False,
        })
        identifier = validate_playlist(playlist, owner, protected_id)
    spotify.settings.save(**{PLAYLIST_ID_KEY: identifier})
    return identifier, not existing


def append_tracks(spotify, uris):
    identifier, created = find_playlist(spotify)
    path = f"/playlists/{identifier}/items"
    existing = set()
    if not created:
        for row in spotify.items(path, params={"limit": 50, "additional_types": "track,episode"}):
            item = row.get("item") or row.get("track") or {}
            existing.update((item.get("uri"), (item.get("linked_from") or {}).get("uri")))
    pending = [uri for uri in uris if uri not in existing]
    for offset in range(0, len(pending), PLAYLIST_BATCH_SIZE):
        spotify.call("POST", path, body={"uris": pending[offset:offset + PLAYLIST_BATCH_SIZE]})
    url = f"https://open.spotify.com/playlist/{identifier}"
    if pending:
        LOG.info("%s に %s 曲を追加しました。%s", PLAYLIST_NAME, len(pending), url)
    else:
        LOG.info("%s の候補曲はすべて登録済みです。%s", PLAYLIST_NAME, url)


def main(argv=None):
    parser = argparse.ArgumentParser(description="My Energy Mix")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="検索結果を表示します")
    mode.add_argument("--apply", action="store_true", help="Spotify のプレイリストに追記します")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    spotify = None
    try:
        songs = load_songs(TRACKS_PATH)
        with run_lock(ENV_PATH):
            spotify = Spotify(Settings(ENV_PATH))
            uris = resolve_songs(spotify, songs, dry_run=args.dry_run)
            if args.apply:
                append_tracks(spotify, uris)
    except MonitorError as error:
        LOG.error("%s", error)
        return 1
    except OSError as error:
        LOG.error("ローカルファイルへのアクセスでエラーが発生しました（errno=%s）", error.errno)
        return 1
    except (KeyError, TypeError, ValueError, IndexError):
        LOG.error("Spotify の応答形式が不正です。")
        return 1
    except KeyboardInterrupt:
        return 130
    finally:
        counts = spotify.request_counts if spotify is not None else {}
        LOG.info("Spotify API 呼び出し合計 %s 回（曲検索 %s 回）", sum(counts.values()), counts.get("GET /v1/search", 0))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
