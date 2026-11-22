# Spotify Release Monitor

Spotify でフォローしているアーティストの新曲を集め、非公開プレイリスト「My New Releases」を更新する Python CLI です。Python 3.10 以上と OS のタイムゾーンデータを使います。外部 Python パッケージのインストールは不要です。

## Spotify Developer Dashboard の設定

1. Spotify Premium が有効なアカウントで [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) にサインインし、**Create app** を選びます。利用する API は **Web API** です。
2. **Redirect URIs** に `http://127.0.0.1:8888/callback` を追加し、設定を保存します。
3. アプリの設定画面から **Client ID** を控えます。OAuth Authorization Code with PKCE を使用するため、Client Secret の入力は不要です。
4. アプリ所有者とは別のアカウントで利用する場合は、アプリの **Settings → Users Management → Add new user** から、利用者の名前と Spotify アカウントのメールアドレスを登録します。

Development Mode ではアプリ所有者の Spotify Premium が必要です。 [Development Mode の要件](https://developer.spotify.com/documentation/web-api/concepts/quota-modes) · [リダイレクト URI の要件](https://developer.spotify.com/documentation/web-api/concepts/redirect_uri)

## Mac での初回認証と実行

プロジェクトのディレクトリで次を実行します。

```sh
python3 --version
cp .env.example .env
chmod 600 .env
```

`.env` の `SPOTIFY_CLIENT_ID=` に Dashboard の Client ID を記入します。設定は `KEY=VALUE` 形式で、値は引用符で囲まずに入力します。

```sh
python3 release_monitor.py auth
python3 release_monitor.py --dry-run
python3 release_monitor.py
```

`auth` でブラウザが開いたら、フォロー情報を収集したい Spotify アカウントでアクセスを許可します。ブラウザが自動で開かない場合はターミナルに表示された URL を開きます。認証に成功するとリフレッシュトークンが `.env` に保存されます。要求する権限は `user-follow-read`、所有者の照合に使う `user-read-private`、`playlist-read-private`、`playlist-modify-private` です。 [Spotify の権限一覧](https://developer.spotify.com/documentation/web-api/concepts/scopes)

`--dry-run` は日付、アーティスト名、曲名、Spotify リンクを標準出力に表示します。通常実行と同じ収集を行い、プレイリストの作成と更新を省きます。アクセストークンは実行時に取得し、Spotify が新しいリフレッシュトークンを返した場合は `.env` に保存します。ログは標準エラー出力に出ます。

初回の通常実行で非公開プレイリストを作成し、以降は `.env` の `SPOTIFY_PLAYLIST_ID` で同じプレイリストを参照します。ID が空の場合は、認証中のアカウントが所有し、説明欄に `Managed by Spotify Release Monitor.` とあるプレイリストを探します。プレイリストの ID を控える場合は、Spotify の共有リンクの `/playlist/` に続く部分を記録します。`.env` は Git 管理対象外です。

## 収集と更新のルール

| `.env` の設定 | デフォルト | 用途 |
| --- | --- | --- |
| `LOOKBACK_DAYS` | `365` | `Asia/Nicosia` の当日を含む直近の日数 |
| `MAX_TRACKS` | `50` | 重複排除後に同期する曲数の上限 |

どちらも1以上の整数を指定します。未設定または空欄の場合はデフォルトを使います。

- フォロー中の全アーティストを取得し、各アーティストのシングルとアルバムを `album,single` の先頭1ページ（最大10件）まで調べます。客演枠の `appears_on` は対象外です。日単位の発売日が設定され、フォロー中のアーティストが1組以上クレジットされている曲を対象とします。 [Spotify のアーティスト別リリース API](https://developer.spotify.com/documentation/web-api/reference/get-an-artists-albums)
- 取得範囲内で発売日の新しい順に曲を調べ、トラック URI が同じ曲、または正規化した曲名とクレジットされたアーティスト ID の集合が一致する曲を重複として扱います。曲名の比較では Unicode の表記幅、大文字・小文字、空白の差を吸収します。同日のリリースはリリース ID 順、同じリリース内は Spotify が返す曲順です。重複排除後の `MAX_TRACKS` 曲（既定で50曲）に達した時点で取得を打ち切ります。`--dry-run` にも同じ曲が表示されます。
- アルバムの曲一覧をページ単位で `.album-tracks-cache.json` に48時間キャッシュし、有効なページは再取得せずに再利用します。`--dry-run` でもキャッシュを使います。このファイルは Git 管理対象外です。ローカルの Spotify 認証情報が変わると再利用範囲も切り替わります。収集中に Spotify がリフレッシュトークンを更新した場合は、同じ認証のキャッシュを引き継ぎます。
- 収集に失敗すると、既存のプレイリストを維持して終了コード `1` を返します。収集が成功して対象が0曲の場合、プレイリストは空になります。終了時に API の合計回数と、HTTP メソッド・エンドポイント別の回数をログに記録します。トークン取得、失敗したリクエスト、再試行も集計に含まれます。
- 曲の内容と順序が同じなら書き込みを省略します。100曲を超える更新は Spotify API の上限に合わせて複数回に分けます。更新の途中でエラーが発生した場合は、保存しておいた更新前の曲順へ復元を試みます。Spotify 側で通信障害や利用枠超過が続く場合、復元にも失敗する可能性があります。
- 通常の HTTP `429` は `Retry-After` を尊重し、待機が300秒以内の場合は最大4回再試行します。`QUOTA_EXCEEDED` は再試行しません。収集中に発生した場合は即時終了し、プレイリストを維持します。複数回に分けた更新ですでに書き込みが始まっている場合は、更新前の内容への復元を試みます。Development Mode の利用枠は開発者アカウント内のアプリ間で共有されます。 [Spotify の利用枠とレート制限](https://developer.spotify.com/blog/2026-07-23-web-api-quota-updates)

## Ubuntu VPS へのデプロイ

以下では `ubuntu@vps.example` を VPS の SSH 接続先に置き換えます。専用ディレクトリを作り、Mac で認証済みの `.env` とアプリのファイルを転送します。

```sh
ssh ubuntu@vps.example 'install -d -m 700 ~/spotify-release-monitor'
scp release_monitor.py spotify_api.py album_track_cache.py systemd/*.service systemd/*.timer ubuntu@vps.example:~/spotify-release-monitor/
scp -p .env ubuntu@vps.example:~/spotify-release-monitor/.env
ssh ubuntu@vps.example
```

VPS で以下を実行します。`enable-linger` により、SSH を切断した後もユーザーの systemd タイマーが動作します。

```sh
sudo apt-get update
sudo apt-get install -y python3 tzdata
cd ~/spotify-release-monitor
chmod 600 .env
/usr/bin/python3 release_monitor.py --dry-run

install -d ~/.config/systemd/user
install -m 644 spotify-release-monitor.service spotify-release-monitor.timer ~/.config/systemd/user/
sudo loginctl enable-linger "$(id -un)"
systemctl --user daemon-reload
systemd-analyze calendar '*-*-* 08:00:00 Asia/Nicosia'
systemctl --user start spotify-release-monitor.service
systemctl --user enable --now spotify-release-monitor.timer
systemctl --user list-timers spotify-release-monitor.timer
journalctl --user -u spotify-release-monitor.service -n 50 --no-pager
```

タイマーは VPS 自体のタイムゾーン設定にかかわらず、`Asia/Nicosia` の毎朝08:00に起動します。夏時間も反映されます。停止中に予定時刻を過ぎた場合は、タイマーの再起動時に実行します。 [systemd のカレンダー構文](https://www.freedesktop.org/software/systemd/man/latest/systemd.time.html) · [systemd.timer](https://www.freedesktop.org/software/systemd/man/latest/systemd.timer.html)

### VPS での再認証

Spotify のリフレッシュトークンは認証から6か月で期限が切れます。期限切れや認証取り消しが発生すると、ログに `auth` の再実行が必要であることが記録されます。Mac で次の SSH コマンドを実行し、表示された Spotify の URL を Mac のブラウザで開きます。この操作は VPS の `.env` にあるプレイリスト ID を保持したままトークンを更新します。 [Spotify のトークン有効期限](https://developer.spotify.com/blog/2026-06-18-refresh-token-expiration)

```sh
ssh -t -o ExitOnForwardFailure=yes -L 8888:127.0.0.1:8888 ubuntu@vps.example \
  'cd ~/spotify-release-monitor && /usr/bin/python3 release_monitor.py auth'
ssh ubuntu@vps.example 'systemctl --user start spotify-release-monitor.service'
```

API が `403` を返す場合は、開発者アカウントの Premium、Dashboard の利用許可、認証した Spotify アカウント、対象プレイリストの所有者を確認してください。VPS への移行後に Mac から確認する場合は `--dry-run` を使い、通常実行は VPS 側に統一してください。

## テスト

```sh
python3 -m unittest discover -s tests
```

模擬 API で取得範囲、重複排除、キャッシュの再利用と期限切れ、API 呼び出しの集計、利用枠超過時のプレイリスト保護を確認します。
