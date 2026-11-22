"""Temporary, local cache for Spotify album track pages."""

import hashlib
import json
import logging
import os
from pathlib import Path
import tempfile
import time
from urllib.parse import urlencode


CACHE_FILENAME = ".album-tracks-cache.json"
CACHE_TTL_SECONDS = 48 * 60 * 60
LOG = logging.getLogger(__name__)


class AlbumTrackCache:
    def __init__(self, spotify):
        self.spotify = spotify
        self.path = spotify.settings.path.with_name(CACHE_FILENAME)
        self.scope = self.credential_scope()
        self.pages = {}
        self.dirty = False

    def credential_scope(self):
        # Track availability depends on the authenticated user's Spotify market.
        settings = self.spotify.settings
        credentials = f"{settings.get('SPOTIFY_CLIENT_ID')}\0{settings.get('SPOTIFY_REFRESH_TOKEN')}"
        return hashlib.sha256(credentials.encode("utf-8")).hexdigest()

    def __enter__(self):
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("pages"), dict):
                raise ValueError("Unexpected cache format")
        except FileNotFoundError:
            return self
        except (OSError, ValueError) as error:
            LOG.warning("アルバム曲一覧のキャッシュを読み込めませんでした（%s）", type(error).__name__)
            return self

        if saved.get("scope") == self.scope:
            now = time.time()
            for key, entry in saved["pages"].items():
                if (isinstance(entry, dict) and isinstance(entry.get("expires_at"), (int, float))
                        and now < entry["expires_at"] <= now + CACHE_TTL_SECONDS
                        and isinstance(entry.get("page"), dict)
                        and isinstance(entry["page"].get("items"), list)
                        and "next" in entry["page"]):
                    self.pages[key] = entry
        self.dirty = saved.get("scope") != self.scope or len(self.pages) != len(saved["pages"])
        return self

    def __exit__(self, exc_type, exc, traceback):
        scope = self.credential_scope()
        if not self.dirty and scope == self.scope:
            return
        try:
            temporary = tempfile.NamedTemporaryFile(
                dir=self.path.parent, prefix=f"{self.path.name}.", mode="w", encoding="utf-8", delete=False,
            )
            try:
                with temporary:
                    json.dump({"scope": scope, "pages": self.pages}, temporary, ensure_ascii=False)
                os.replace(temporary.name, self.path)
            finally:
                Path(temporary.name).unlink(missing_ok=True)
        except OSError as error:
            LOG.warning("アルバム曲一覧のキャッシュを保存できませんでした（%s）", type(error).__name__)

    def load_page(self, method, path, *, params=None):
        key = f"{path}?{urlencode(params)}" if params else path
        cached = self.pages.get(key)
        if cached and cached["expires_at"] > time.time():
            return cached["page"]

        page = self.spotify.call(method, path, params=params)
        following = page.get("next")
        if (not isinstance(page.get("items"), list) or "next" not in page
                or (following is not None and (not isinstance(following, str) or not following))):
            return page  # Let Spotify.items report malformed responses.
        compact = {"items": [], "next": following}
        try:
            for track in page["items"]:
                if not isinstance(track["uri"], str) or not isinstance(track["name"], str):
                    return page
                compact["items"].append({
                    "uri": track["uri"], "name": track["name"],
                    "artists": [{"id": artist["id"], "name": artist["name"]} for artist in track["artists"]],
                    "is_local": bool(track.get("is_local")), "is_playable": track.get("is_playable"),
                    "restrictions": bool(track.get("restrictions")),
                })
        except (KeyError, TypeError):
            return page
        self.pages[key] = {"expires_at": time.time() + CACHE_TTL_SECONDS, "page": compact}
        self.dirty = True
        return page
