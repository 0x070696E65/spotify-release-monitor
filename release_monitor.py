"""Collect recent releases from followed artists and sync a Spotify playlist."""

import argparse
from dataclasses import dataclass
from datetime import date, datetime
import json
import logging
from pathlib import Path
import re
import sys
import unicodedata
from zoneinfo import ZoneInfo

from album_track_cache import AlbumTrackCache
from spotify_api import ApiError, MonitorError, Settings, Spotify, authorize, run_lock


ENV_PATH = Path(__file__).resolve().with_name(".env")
EXCLUDE_KEYWORDS_PATH = ENV_PATH.with_name("exclude_keywords.json")
TIME_ZONE = "Asia/Nicosia"
DEFAULT_LOOKBACK_DAYS = 365
DEFAULT_MAX_TRACKS = 50
PLAYLIST_NAME = "My New Releases"
PLAYLIST_DESCRIPTION = "Managed by Spotify Release Monitor."
PLAYLIST_BATCH_SIZE = 100
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReleaseTrack:
    uri: str
    released: date
    title: str
    artists: str


def normalize_title(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def reject_json_constant(value):
    raise ValueError(f"JSON で使用できない値 {value}")


class TitleExclusions:
    def __init__(self, path, *, dry_run):
        try:
            config = json.loads(path.read_text(encoding="utf-8"), parse_constant=reject_json_constant)
        except FileNotFoundError:
            raise MonitorError(f"除外キーワード設定ファイルが見つかりません。{path}") from None
        except json.JSONDecodeError as error:
            raise MonitorError(f"{path.name} の JSON が不正です（{error.lineno}行目、{error.colno}列目）。") from None
        except (OSError, UnicodeError):
            raise MonitorError(f"{path.name} を読み込めません。UTF-8 とファイルの権限を確認してください。") from None
        except ValueError as error:
            raise MonitorError(f"{path.name} の JSON が不正です（{error}）。") from None

        keywords = config.get("exclude_keywords") if isinstance(config, dict) else None
        if not isinstance(keywords, list):
            raise MonitorError(f"{path.name} の exclude_keywords には文字列の配列を指定してください。")

        patterns = {}
        for index, keyword in enumerate(keywords):
            normalized = normalize_title(keyword) if isinstance(keyword, str) else ""
            if not normalized:
                raise MonitorError(f"{path.name} の exclude_keywords[{index}] には空白以外を含む文字列を指定してください。")
            literal = re.escape(normalized)
            # Japanese titles can write version names without surrounding spaces.
            if not re.search(r"[\u3040-\u30ff\u3400-\u9fff]", normalized):
                literal = rf"(?<!\w){literal}(?!\w)"
            patterns.setdefault(normalized, (keyword, re.compile(literal)))

        self.patterns = tuple(patterns.values())
        self.counts = {keyword: 0 for keyword, _ in self.patterns}
        self.total = 0
        self.dry_run = dry_run

    def excludes(self, normalized_title, *, title, artists):
        matches = [keyword for keyword, pattern in self.patterns if pattern.search(normalized_title)]
        if not matches:
            return False
        self.total += 1
        for keyword in matches:
            self.counts[keyword] += 1
        if self.dry_run:
            LOG.info("除外曲 %r / アーティスト %r / 一致キーワード %s", title, artists,
                     ", ".join(repr(keyword) for keyword in matches))
        return True


def positive_setting(settings, key, default):
    value = settings.get(key)
    if not value:
        return default
    try:
        number = int(value) if re.fullmatch(r"[0-9]+", value) else 0
    except ValueError:
        number = 0
    if number < 1:
        raise MonitorError(f".env の {key} には 1 以上の整数を設定してください。")
    return number


def collect_tracks(spotify, today, *, lookback_days=DEFAULT_LOOKBACK_DAYS, max_tracks=DEFAULT_MAX_TRACKS,
                   album_page_loader=None, title_exclusions=None):
    start = date.fromordinal(max(1, today.toordinal() - lookback_days + 1))
    artists = {
        artist["id"] for artist in spotify.items(
            "/me/following", params={"type": "artist", "limit": 50}, envelope="artists",
        )
    }
    LOG.info("フォロー中のアーティスト %s 組。収集期間 %s ～ %s", len(artists), start, today)

    albums = {}
    seen_albums = set()
    imprecise_dates = 0
    for artist_id in sorted(artists):
        page = spotify.call("GET", f"/artists/{artist_id}/albums", params={
            "include_groups": "single", "limit": 10,
        })
        if not isinstance(page.get("items"), list):
            raise MonitorError("Spotify のページにリリースの一覧がありません。")
        for album in page["items"][:10]:
            album_id = album["id"]
            if album_id in seen_albums or album["album_type"] != "single":
                continue
            seen_albums.add(album_id)
            if album["release_date_precision"] != "day":
                imprecise_dates += 1
                continue
            released = date.fromisoformat(album["release_date"])
            if start <= released <= today:
                albums[album_id] = released
    LOG.info("対象リリース %s 件。日付が日単位で確定できず除外したリリース %s 件", len(albums), imprecise_dates)

    tracks = []
    seen_uris = set()
    seen_titles_and_artists = set()
    for album_id in sorted(albums, key=lambda key: (-albums[key].toordinal(), key)):
        for track in spotify.items(f"/albums/{album_id}/tracks", params={"limit": 50},
                                   page_loader=album_page_loader):
            credits = track["artists"]
            artist_ids = {artist["id"] for artist in credits}
            if artist_ids.isdisjoint(artists):
                continue
            if track.get("is_local") or track.get("is_playable") is False or track.get("restrictions"):
                continue
            uri = track["uri"]
            if uri in seen_uris:
                continue
            if not re.fullmatch(r"spotify:track:[A-Za-z0-9]+", uri):
                raise MonitorError("収集した楽曲の Spotify URI が不正です。")
            seen_uris.add(uri)
            track_name = track["name"]
            title = normalize_title(track_name)
            title_and_artists = (title, frozenset(artist_ids))
            if title_and_artists in seen_titles_and_artists:
                continue
            seen_titles_and_artists.add(title_and_artists)
            artist_names = ", ".join(artist["name"] for artist in credits)
            if title_exclusions is not None and title_exclusions.excludes(
                title, title=track_name, artists=artist_names,
            ):
                continue
            tracks.append(ReleaseTrack(uri, albums[album_id], track_name, artist_names))
            # All remaining tracks rank behind this one in the release order.
            if len(tracks) == max_tracks:
                return tracks
    return tracks


def playlist_id(spotify):
    owner = spotify.call("GET", "/me")["id"]
    saved_id = spotify.settings.get("SPOTIFY_PLAYLIST_ID")
    if saved_id:
        if not re.fullmatch(r"[A-Za-z0-9]+", saved_id):
            raise MonitorError(".env の SPOTIFY_PLAYLIST_ID が不正です。")
        playlist = spotify.call("GET", f"/playlists/{saved_id}", params={"fields": "owner(id)"})
        if playlist["owner"]["id"] != owner:
            raise MonitorError("保存済みプレイリストの所有者が、認証中のアカウントと一致しません。")
        return saved_id

    matches = [
        playlist["id"] for playlist in spotify.items("/me/playlists", params={"limit": 50})
        if playlist.get("description") == PLAYLIST_DESCRIPTION and playlist["owner"]["id"] == owner
    ]
    if len(matches) > 1:
        raise MonitorError("管理対象のプレイリストが複数あります。.env に対象の SPOTIFY_PLAYLIST_ID を設定してください。")
    if matches:
        identifier = matches[0]
    else:
        playlist = spotify.call("POST", "/me/playlists", body={
            "name": PLAYLIST_NAME, "description": PLAYLIST_DESCRIPTION, "public": False,
        })
        identifier = playlist["id"]
        LOG.info("%s を作成しました", PLAYLIST_NAME)
    spotify.settings.save(SPOTIFY_PLAYLIST_ID=identifier)
    return identifier


def current_playlist_uris(spotify, path):
    uris = []
    for row in spotify.items(path, params={"limit": 50, "additional_types": "track,episode"}):
        item = row.get("item") or row.get("track") or {}
        uri = (item.get("linked_from") or {}).get("uri") or item.get("uri", "")
        if row.get("is_local") or not re.fullmatch(r"spotify:(track|episode):[A-Za-z0-9]+", uri):
            raise MonitorError("既存プレイリストに復元できない項目があるため、更新を中止しました。")
        uris.append(uri)
    return uris


def sync_playlist(spotify, tracks):
    identifier = playlist_id(spotify)
    path = f"/playlists/{identifier}/items"
    url = f"https://open.spotify.com/playlist/{identifier}"
    previous = current_playlist_uris(spotify, path)
    desired = [track.uri for track in tracks]
    if desired == previous:
        LOG.info("プレイリストは最新です（%s 曲） %s", len(desired), url)
        return

    spotify_write_started = False
    try:
        spotify.call("PUT", path, body={"uris": desired[:PLAYLIST_BATCH_SIZE]})
        spotify_write_started = True
        for offset in range(PLAYLIST_BATCH_SIZE, len(desired), PLAYLIST_BATCH_SIZE):
            spotify.call("POST", path, body={"uris": desired[offset:offset + PLAYLIST_BATCH_SIZE]})
    except (MonitorError, OSError, ValueError, KeyError, TypeError) as error:
        ambiguous = isinstance(error, ApiError) and (error.status is None or error.status >= 500)
        if spotify_write_started or ambiguous:
            LOG.warning("更新に失敗しました。更新前の曲順への復元を試みます")
            try:
                if current_playlist_uris(spotify, path) != previous:
                    spotify.call("PUT", path, body={"uris": previous[:PLAYLIST_BATCH_SIZE]})
                    for offset in range(PLAYLIST_BATCH_SIZE, len(previous), PLAYLIST_BATCH_SIZE):
                        spotify.call("POST", path, body={"uris": previous[offset:offset + PLAYLIST_BATCH_SIZE]})
                LOG.info("更新前の曲順を確認または復元しました")
            except (MonitorError, OSError, ValueError, KeyError, TypeError):
                raise MonitorError(f"更新と復元に失敗しました。プレイリストを確認してください。{url}") from None
        raise
    LOG.info("%s を更新しました（%s 曲） %s", PLAYLIST_NAME, len(desired), url)


def run(spotify, dry_run):
    try:
        exclusions = TitleExclusions(EXCLUDE_KEYWORDS_PATH, dry_run=dry_run)
        lookback_days = positive_setting(spotify.settings, "LOOKBACK_DAYS", DEFAULT_LOOKBACK_DAYS)
        max_tracks = positive_setting(spotify.settings, "MAX_TRACKS", DEFAULT_MAX_TRACKS)
        with AlbumTrackCache(spotify) as cache:
            tracks = collect_tracks(spotify, datetime.now(ZoneInfo(TIME_ZONE)).date(),
                                    lookback_days=lookback_days, max_tracks=max_tracks,
                                    album_page_loader=cache.load_page, title_exclusions=exclusions)
    except (MonitorError, OSError, ValueError, KeyError, TypeError):
        LOG.error("収集を完了できなかったため、プレイリストは変更していません。")
        raise
    LOG.info("除外キーワードによる除外 %s 曲", exclusions.total)
    LOG.info("キーワード別の除外件数 %s",
             ", ".join(f"{keyword!r}={count}" for keyword, count in exclusions.counts.items()) or "設定なし")
    LOG.info("収集完了。プレイリスト対象 %s 曲（上限 %s 曲）", len(tracks), max_tracks)
    if dry_run:
        for track in tracks:
            track_id = track.uri.rsplit(":", 1)[-1]
            print(f"{track.released}  {track.artists} — {track.title}  https://open.spotify.com/track/{track_id}")
    else:
        sync_playlist(spotify, tracks)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Spotify Release Monitor")
    parser.add_argument("command", nargs="?", choices=("run", "auth"), default="run")
    parser.add_argument("--dry-run", action="store_true", help="収集した楽曲を表示して終了します")
    args = parser.parse_args(argv)
    if args.command == "auth" and args.dry_run:
        parser.error("--dry-run は run で利用してください。")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    spotify = None
    try:
        with run_lock(ENV_PATH):
            spotify = Spotify(Settings(ENV_PATH))
            if args.command == "auth":
                authorize(spotify)
            else:
                run(spotify, args.dry_run)
    except MonitorError as error:
        LOG.error("%s", error)
        return 1
    except OSError as error:
        LOG.error("ファイルまたはローカル通信でエラーが発生しました（errno=%s）", error.errno)
        return 1
    except (ValueError, KeyError, TypeError):
        LOG.error("設定、日付、または Spotify の応答形式が不正です。")
        return 1
    except KeyboardInterrupt:
        LOG.warning("処理を中断しました")
        return 130
    finally:
        counts = spotify.request_counts if spotify is not None else {}
        LOG.info("Spotify API 呼び出し合計 %s 回", sum(counts.values()))
        for endpoint, count in sorted(counts.items()):
            LOG.info("Spotify API %s: %s 回", endpoint, count)
    return 0


if __name__ == "__main__":
    sys.exit(main())
