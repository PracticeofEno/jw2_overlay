# jw2_result_server

Small HTTP server that receives Jwar2 `.ply` replay files, analyzes them with
`jw2_result_server/jwar2_replay_result.py`, and stores persistent win/loss stats
in JSON files.

## Run

```powershell
python jw2_result_server\server.py --host 127.0.0.1 --port 8765
```

Run the overlay with the default server URL:

```powershell
python ranker\jw2_overlay.py
```

When the overlay auto-saves a finished game replay, it posts the saved `.ply`
to `http://127.0.0.1:8765/replay`. Use `--result-server-url` to point at a
different server, or `--no-result-upload` to disable the upload.

## Endpoints

- `GET /all` or `GET /:all`: shows an HTML table for all known players.
- `GET /all?format=json`: returns all known players as JSON.
- `GET /{nickname}` or `GET /:{nickname}`: returns one player's wins, losses,
  games, and win rate.
- `POST /replay`: uploads a replay. The server accepts raw `.ply` bytes,
  multipart form data with a `file`, `replay`, or `ply` field, or JSON with a
  `ply_base64`, `replay_base64`, or `file_base64` field.

## Storage

- `data/replay_hashes.json`: one compact entry per accepted replay SHA-256 hash.
- `data/player_stats.json`: nickname, wins, losses, games, and win rate.

Duplicate replay hashes are accepted as requests, but they do not update stats
again.
