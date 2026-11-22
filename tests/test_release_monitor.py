from contextlib import redirect_stdout
from datetime import date, timedelta
from email.message import Message
from io import BytesIO, StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, call, patch
from urllib.error import HTTPError
from urllib.parse import urlsplit

from album_track_cache import AlbumTrackCache, CACHE_FILENAME, CACHE_TTL_SECONDS
from release_monitor import ReleaseTrack, collect_tracks, main, run, sync_playlist
from spotify_api import API, ApiError, MonitorError, Settings, Spotify


TODAY = date(2026, 11, 22)
PLAYLIST_PATH = "/playlists/Target/items"


def page(*items, next_path=None):
    return {"items": list(items), "next": API + next_path if next_path else None}


def album(identifier, days_ago):
    return {"id": identifier, "album_type": "single", "release_date_precision": "day",
            "release_date": (TODAY - timedelta(days=days_ago)).isoformat()}


def track(identifier, *artists, name=None):
    return {"uri": f"spotify:track:{identifier}", "name": name or identifier,
            "artists": [{"id": artist, "name": artist} for artist in artists or ("A",)]}


def release_track(identifier):
    return ReleaseTrack(f"spotify:track:{identifier}", TODAY, identifier, "A")


class ReleaseMonitorTests(unittest.TestCase):
    def make_spotify(self, settings=""):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        env = Path(directory.name) / ".env"
        env.write_text(f"SPOTIFY_CLIENT_ID=test\nSPOTIFY_REFRESH_TOKEN=initial\n{settings}", encoding="utf-8")
        return Spotify(Settings(env))

    def test_top_tracks_use_one_release_page_per_followed_artist(self):
        spotify = self.make_spotify()
        responses = {
            "/me/following": {"artists": page({"id": "A"}, next_path="/me/following?after=A")},
            API + "/me/following?after=A": {"artists": page({"id": "B"})},
            "/artists/A/albums": page(album("Older", 60), album("New", 0),
                                     next_path="/artists/A/albums?offset=10"),
            "/artists/B/albums": page(album("Tie", 0), album("Older", 60)),
            "/albums/New/tracks": page(track("Foreign", "X"), track("Newest", "A", "B", name="Ｎｅｗ  Song"),
                                      next_path="/albums/New/tracks?offset=50"),
            API + "/albums/New/tracks?offset=50": page(track("Alias", "B", "A", name="new song"),
                                                      track("Second", "B", name="New Song")),
            "/albums/Tie/tracks": page(track("Newest", name="Changed metadata"), track("Third"),
                                      next_path="/albums/Tie/tracks?offset=50"),
        }
        spotify.call = Mock(side_effect=lambda method, path, **kwargs: responses[path])

        tracks = collect_tracks(spotify, TODAY, max_tracks=3)

        self.assertEqual([item.uri for item in tracks], ["spotify:track:Newest", "spotify:track:Second",
                                                       "spotify:track:Third"])
        self.assertEqual([item.args[1] for item in spotify.call.call_args_list], list(responses))
        for identifier in ("A", "B"):
            spotify.call.assert_any_call("GET", f"/artists/{identifier}/albums",
                                         params={"include_groups": "album,single", "limit": 10})

    def test_default_limit_is_fifty(self):
        spotify = self.make_spotify()
        responses = {
            "/me/following": {"artists": page({"id": "A"})},
            "/artists/A/albums": page(album("New", 0)),
            "/albums/New/tracks": page(*(track(f"T{i}") for i in range(50)),
                                      next_path="/albums/New/tracks?offset=50"),
        }
        spotify.call = Mock(side_effect=lambda method, path, **kwargs: responses[path])

        tracks = collect_tracks(spotify, TODAY)

        self.assertEqual(len(tracks), 50)
        self.assertEqual(spotify.call.call_count, 3)

    def test_default_window_includes_today_and_the_364th_day_before_it(self):
        spotify = self.make_spotify()
        responses = {
            "/me/following": {"artists": page({"id": "A"})},
            "/artists/A/albums": page(album("Future", -1), album("OutOfRange", 365),
                                      album("Boundary", 364), album("Today", 0)),
            "/albums/Today/tracks": page(track("Today")),
            "/albums/Boundary/tracks": page(track("Boundary")),
        }
        spotify.call = Mock(side_effect=lambda method, path, **kwargs: responses[path])

        tracks = collect_tracks(spotify, TODAY)

        self.assertEqual([item.title for item in tracks], ["Today", "Boundary"])

        spotify.call.reset_mock()
        tracks = collect_tracks(spotify, TODAY, lookback_days=1)

        self.assertEqual([item.title for item in tracks], ["Today"])
        self.assertNotIn(call("GET", "/albums/Boundary/tracks", params={"limit": 50}),
                         spotify.call.call_args_list)

    def test_run_uses_configured_settings_for_normal_and_dry_run(self):
        spotify = self.make_spotify("LOOKBACK_DAYS=14\nMAX_TRACKS=1\n")
        selected = [release_track("Chosen")]
        output = StringIO()
        with patch("release_monitor.collect_tracks", return_value=selected) as collect, \
             patch("release_monitor.sync_playlist") as sync, redirect_stdout(output):
            run(spotify, dry_run=True)
            sync.assert_not_called()
            self.assertIn("https://open.spotify.com/track/Chosen", output.getvalue())
            run(spotify, dry_run=False)
            sync.assert_called_once_with(spotify, selected)
        self.assertEqual([{key: value for key, value in entry.kwargs.items() if key != "album_page_loader"}
                          for entry in collect.call_args_list],
                         [{"lookback_days": 14, "max_tracks": 1}] * 2)

    def test_missing_or_empty_settings_use_defaults(self):
        spotify = self.make_spotify("MAX_TRACKS=\n")
        with patch("release_monitor.collect_tracks", return_value=[]) as collect:
            run(spotify, dry_run=True)
        self.assertEqual(collect.call_args.kwargs["lookback_days"], 365)
        self.assertEqual(collect.call_args.kwargs["max_tracks"], 50)

    def test_invalid_settings_prevent_api_calls(self):
        for key, value in (("LOOKBACK_DAYS", "0"), ("MAX_TRACKS", "-1"), ("MAX_TRACKS", "abc")):
            with self.subTest(key=key, value=value):
                spotify = self.make_spotify(f"{key}={value}\n")
                spotify.call = Mock()
                with self.assertLogs("release_monitor", "ERROR"), self.assertRaisesRegex(MonitorError, key):
                    run(spotify, dry_run=False)
                spotify.call.assert_not_called()

    def test_album_cache_reuses_fetched_pages_and_refetches_after_expiry(self):
        spotify = self.make_spotify()
        first_path = "/albums/New/tracks"
        second_path = API + "/albums/New/tracks?offset=50"
        responses = {
            "/me/following": {"artists": page({"id": "A"})},
            "/artists/A/albums": page(album("New", 0)),
            first_path: page(track("First"), next_path="/albums/New/tracks?offset=50"),
            second_path: page(track("Second")),
        }
        spotify.call = Mock(side_effect=lambda method, path, **kwargs: responses[path])
        with patch("album_track_cache.time.time", return_value=1000) as clock:
            with AlbumTrackCache(spotify) as cache:
                tracks = collect_tracks(spotify, TODAY, max_tracks=1, album_page_loader=cache.load_page)
            self.assertEqual([item.title for item in tracks], ["First"])
            self.assertEqual(spotify.call.call_args_list[-1].args[1], first_path)

            spotify.call.reset_mock()
            with AlbumTrackCache(spotify) as cache:
                tracks = collect_tracks(spotify, TODAY, max_tracks=2, album_page_loader=cache.load_page)
            self.assertEqual([item.title for item in tracks], ["First", "Second"])
            self.assertEqual([entry.args[1] for entry in spotify.call.call_args_list],
                             ["/me/following", "/artists/A/albums", second_path])

            clock.return_value = 1000 + CACHE_TTL_SECONDS
            spotify.call.reset_mock()
            with AlbumTrackCache(spotify) as cache:
                collect_tracks(spotify, TODAY, max_tracks=2, album_page_loader=cache.load_page)
            self.assertEqual([entry.args[1] for entry in spotify.call.call_args_list], list(responses))

    def test_album_cache_keeps_the_scope_on_token_rotation_and_separates_credentials(self):
        spotify = self.make_spotify()
        path = "/albums/New/tracks"
        response = page(track("Track"))
        spotify.call = Mock(return_value=response)
        with AlbumTrackCache(spotify) as cache:
            cache.load_page("GET", path)
            spotify.settings.save(SPOTIFY_REFRESH_TOKEN="rotated")

        spotify.call.reset_mock()
        with AlbumTrackCache(spotify) as cache:
            self.assertEqual(cache.load_page("GET", path)["items"][0]["name"], "Track")
        spotify.call.assert_not_called()
        stored = spotify.settings.path.with_name(CACHE_FILENAME).read_text(encoding="utf-8")
        self.assertNotIn("rotated", stored)

        spotify.settings.save(SPOTIFY_REFRESH_TOKEN="another-account")
        with AlbumTrackCache(spotify) as cache:
            cache.load_page("GET", path)
        spotify.call.assert_called_once_with("GET", path, params=None)

    def test_dry_run_with_27_artists_reuses_album_tracks_on_the_next_run(self):
        spotify = self.make_spotify()

        def open_request(request, *, timeout):
            path = urlsplit(request.full_url).path
            if path == "/api/token":
                body = {"access_token": "test-token", "expires_in": 3600}
            elif path == "/v1/me/following":
                body = {"artists": page(*({"id": f"A{i}"} for i in range(27)))}
            elif path.startswith("/v1/artists/"):
                body = page(album("Shared", 0), next_path=f"{path.removeprefix('/v1')}?offset=10")
            else:
                self.assertEqual(path, "/v1/albums/Shared/tracks")
                body = page(track("SharedTrack", "A0"))
            return BytesIO(json.dumps(body).encode())

        with patch("release_monitor.ENV_PATH", spotify.settings.path), \
             patch("release_monitor.datetime") as now, \
             patch("spotify_api.OPENER.open", side_effect=open_request) as http:
            now.now.return_value.date.return_value = TODAY
            for expected_total in (30, 29):
                with self.subTest(call_count=expected_total), self.assertLogs(level="INFO") as logs, \
                     redirect_stdout(StringIO()) as stdout:
                    http.reset_mock()
                    self.assertEqual(main(["--dry-run"]), 0)
                    self.assertIn("https://open.spotify.com/track/SharedTrack", stdout.getvalue())
                    self.assertEqual(http.call_count, expected_total)
                    self.assertIn("GET /v1/artists/{id}/albums: 27 回", "\n".join(logs.output))

    def test_quota_error_exits_without_retry_and_logs_actual_endpoint_totals(self):
        spotify = self.make_spotify()
        visited = []

        def open_request(request, *, timeout):
            path = urlsplit(request.full_url).path
            visited.append(path)
            if path == "/api/token":
                body = {"access_token": "test-token", "expires_in": 3600}
            elif path == "/v1/me/following":
                body = {"artists": page({"id": "A"}, {"id": "B"}, {"id": "C"})}
            elif path == "/v1/artists/A/albums" and visited.count(path) == 2:
                body = page()
            else:
                self.assertIn(path, ("/v1/artists/A/albums", "/v1/artists/B/albums"))
                headers = Message()
                headers["Retry-After"] = "2"
                body = {} if path.endswith("/A/albums") else {"error": {"reason": "QUOTA_EXCEEDED"}}
                raise HTTPError(request.full_url, 429, "limit", headers, BytesIO(json.dumps(body).encode()))
            return BytesIO(json.dumps(body).encode())

        with patch("release_monitor.ENV_PATH", spotify.settings.path), \
             patch("spotify_api.OPENER.open", side_effect=open_request), \
             patch("spotify_api.time.sleep") as sleep, self.assertLogs(level="INFO") as logs:
            self.assertEqual(main([]), 1)

        self.assertEqual(visited, ["/api/token", "/v1/me/following", "/v1/artists/A/albums",
                                   "/v1/artists/A/albums", "/v1/artists/B/albums"])
        sleep.assert_called_once_with(2)
        for message in ("QUOTA_EXCEEDED", "Spotify API 呼び出し合計 5 回",
                        "GET /v1/artists/{id}/albums: 3 回", "GET /v1/me/following: 1 回",
                        "POST /api/token: 1 回"):
            self.assertIn(message, "\n".join(logs.output))

    def test_catalog_error_prevents_playlist_update(self):
        spotify = self.make_spotify("MAX_TRACKS=1\n")
        spotify.call = Mock(side_effect=[{"artists": page({"id": "A"}, {"id": "B"})},
                                        page(album("New", 0)),
                                        ApiError("unavailable", status=503)])
        with patch("release_monitor.sync_playlist") as sync, self.assertLogs("release_monitor", "ERROR"):
            with self.assertRaises(ApiError):
                run(spotify, dry_run=False)
            sync.assert_not_called()
        self.assertEqual(spotify.call.call_args.args, ("GET", "/artists/B/albums"))

    def test_same_contents_and_order_skip_writes_but_reordering_updates(self):
        spotify = self.make_spotify("SPOTIFY_PLAYLIST_ID=Target\n")
        first, second = release_track("First"), release_track("Second")
        current = page({"item": {"uri": first.uri}}, {"item": {"uri": second.uri}})
        reads = [{"id": "user"}, {"owner": {"id": "user"}}, current]
        spotify.call = Mock(side_effect=reads + reads + [{}])

        sync_playlist(spotify, [first, second])
        self.assertTrue(all(entry.args[0] == "GET" for entry in spotify.call.call_args_list))
        sync_playlist(spotify, [second, first])
        self.assertEqual(spotify.call.call_args, call("PUT", PLAYLIST_PATH, body={"uris": [second.uri, first.uri]}))

    def test_failed_second_batch_restores_original_tracks(self):
        spotify = self.make_spotify("SPOTIFY_PLAYLIST_ID=Target\n")
        original = "spotify:track:Original"
        desired = [release_track(f"T{i}") for i in range(101)]
        spotify.call = Mock(side_effect=[
            {"id": "user"}, {"owner": {"id": "user"}}, page({"item": {"uri": original}}),
            {}, ApiError("timeout"), page({"item": {"uri": desired[0].uri}}), {},
        ])

        with self.assertLogs("release_monitor", "WARNING"), self.assertRaises(ApiError):
            sync_playlist(spotify, desired)

        writes = [entry for entry in spotify.call.call_args_list if entry.args[0] != "GET"]
        self.assertEqual(writes, [
            call("PUT", PLAYLIST_PATH, body={"uris": [item.uri for item in desired[:100]]}),
            call("POST", PLAYLIST_PATH, body={"uris": [desired[100].uri]}),
            call("PUT", PLAYLIST_PATH, body={"uris": [original]}),
        ])
