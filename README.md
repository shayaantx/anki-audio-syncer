# anki-audio-syncer

Adds Japanese text-to-speech audio to every note in an Anki deck, so your cards
can be used for listening practice. Runs headless in Docker and works directly
with your **self-hosted Anki sync server**. No Anki desktop app is needed.

## How it works

1. **Sync down.** Logs in to your sync server and syncs a local copy of your
   collection, kept in a Docker volume.
2. **Synthesise.** For each note in the deck, sends the **Front** text to a
   local [VOICEVOX ENGINE](https://github.com/VOICEVOX/voicevox_engine) and
   encodes the result to mp3.
3. **Edit.** Adds the mp3 to the collection's media and writes a
   `[sound:anki-audio-syncer_….mp3]` tag into the note's **Audio** field.
4. **Sync up.** Normal-syncs the edited notes and the new audio back to the
   server. Your phone and desktop pick them up on their next sync.

Speech synthesis runs entirely on your machine. The only network traffic is to
your own sync server, plus pulling the engine image once.

### Safety

- **It never uploads a whole collection.** It only does normal (incremental)
  syncs upward. Whenever Anki demands a full sync, anki-audio-syncer takes the server's
  copy, because its local copy is just a cache.
- **Dry runs upload nothing.** `--dry-run` syncs down and lists what would
  change.
- **Back up your server data first anyway** (`${SERVER_DIRECTORY}/anki`) before
  the first real run. This is a new tool writing to your only collection.
- **Sync your other devices first.** Normal sync merges changes, but sync any
  pending reviews before running, and again afterwards.

## One-time setup in Anki

Do this in any Anki app (desktop, AnkiDroid, AnkiMobile), then sync:

1. **Add an `Audio` field.** Tools → Manage Note Types → _your note type_ →
   Fields → Add → `Audio`.
2. **Put the audio on a card.** Same note type → Cards, then pick one:
   - Add `{{Audio}}` to the **Back** template: you hear it after flipping.
   - Add `{{Audio}}` to the **Front** template: it plays as the card appears.
   - Add a **new card type** for listening practice. Its front template is just
     `{{Audio}}` and its back is `{{Front}}<br>{{Back}}`. You hear the Japanese
     and try to recall it.
3. **Sync, choosing Upload.** Changing a note type makes Anki ask for a full
   sync. Upload from the device where you made the change, and let the other
   devices download.

Alternatively, skip the field and use `--audio-field Back` to append the audio
to the Back field.

## Models

The VOICEVOX ENGINE image **includes all the standard talk voices**, so there's
nothing to download beyond the image itself:

```bash
docker compose pull      # voicevox/voicevox_engine:cpu-0.25.2 + python base image
```

Models are loaded lazily, so only the voice you use takes up memory.

The voices live in `.vvm` files (the full list is in
[VOICEVOX/voicevox_vvm](https://github.com/VOICEVOX/voicevox_vvm)). For
learning, pick a calm, natural "normal" style rather than the character or
whisper styles:

| Style id | Voice                              | Model file | Notes                             |
| -------- | ---------------------------------- | ---------- | --------------------------------- |
| 14       | 冥鳴ひまり (Meimei Himari)         | `1.vvm`    | Calm female voice, a good default |
| 8        | 春日部つむぎ (Kasukabe Tsumugi)    | `0.vvm`    | Bright, clear female voice        |
| 10       | 雨晴はう (Amehare Hau)             | `0.vvm`    | Soft female voice                 |
| 2        | 四国めたん (Shikoku Metan), normal | `0.vvm`    | Standard female voice             |
| 16       | 九州そら (Kyushu Sora), normal     | `2.vvm`    | Mature female voice               |
| 13       | 青山龍星 (Aoyama Ryusei)           | `15.vvm`   | Deep male voice                   |
| 11       | 玄野武宏 (Genno Takehiro)          | `4.vvm`    | Male voice                        |
| 21       | 剣崎雌雄 (Kenzaki Yuu)             | `4.vvm`    | Male voice                        |

Style ids can change between engine versions, so confirm them with
`--list-voices`.

Each voice has its own terms of use. Personal study is fine. Sharing a deck
with the audio requires crediting `VOICEVOX:<voice name>`, and some voices
(e.g. 青山龍星) have extra conditions for commercial use.

## Usage

```bash
cp .env.example .env               # set ANKI_SYNC_URL, ANKI_USERNAME, ANKI_PASSWORD
docker compose build
docker compose up -d               # start the engine in the background
docker compose run --rm anki-audio-syncer --list-voices
docker compose run --rm anki-audio-syncer --deck "My Japanese Deck" --speaker 14 --dry-run
docker compose run --rm anki-audio-syncer --deck "My Japanese Deck" --speaker 14 --limit 5
docker compose run --rm anki-audio-syncer --deck "My Japanese Deck" --speaker 14
docker compose down                # stop the engine when done
```

After the `--limit 5` run, sync a device and play one of those cards before
doing the whole deck.

- **Credentials.** `ANKI_USERNAME` and `ANKI_PASSWORD` are the same ones in your
  sync server's `SYNC_USER1`. Variables already set in your shell take
  precedence over `.env`.
- **First run.** The first run downloads your whole collection **and all its
  media** into the `anki-audio-syncer-data` volume, which can take a while. Later runs
  only sync changes. `docker volume rm anki-audio-syncer_anki-audio-syncer-data` resets it.
- **Waiting.** The script waits up to 60 s (`--wait`) for the engine to finish
  starting. `docker compose up` only starts the engine; `anki-audio-syncer` runs on
  demand.
- **Sync server on the same machine as Docker.** Use its LAN IP, or
  `http://host.docker.internal:27701`.
- **GPU.** For NVIDIA, change the image in `compose.yaml` to
  `voicevox/voicevox_engine:nvidia-0.25.2` and add `gpus: all` to the
  `voicevox` service.

The engine's interactive API docs are at http://127.0.0.1:50021/docs while it's
running.

### Without Docker

```bash
pip install -r requirements.txt    # or: uv run anki-audio-syncer.py ...
export ANKI_SYNC_URL=http://192.168.1.196:27701 ANKI_USERNAME=... ANKI_PASSWORD=...
python anki-audio-syncer.py --deck "My Japanese Deck" --speaker 14
```

The collection cache goes in `./data` (`--data-dir`). Any engine that
implements VOICEVOX's API works, e.g.
[AivisSpeech Engine](https://github.com/Aivis-Project/AivisSpeech-Engine) with
`--voicevox-url http://127.0.0.1:10101`.

## Options

| Option           | Default                    | What it does                                                                                                   |
| ---------------- | -------------------------- | -------------------------------------------------------------------------------------------------------------- |
| `--deck NAME`    | —                          | Deck to process, including subdecks.                                                                           |
| `--query QUERY`  | —                          | Any Anki search instead of a deck, e.g. `'deck:Japanese added:7'`.                                             |
| `--list-voices`  | —                          | Print the engine's voices and their style ids, then exit.                                                      |
| `--speaker ID`   | first voice                | VOICEVOX style id. Env: `VOICEVOX_SPEAKER`.                                                                    |
| `--speed`        | `1.0`                      | Speaking speed, e.g. `0.85` for slower.                                                                        |
| `--source-field` | `Front`                    | Field whose text is read aloud.                                                                                |
| `--audio-field`  | `Audio`                    | Field the `[sound:]` tag goes in. Can be an existing field such as `Back`; the tag is appended to its content. |
| `--bitrate`      | `64`                       | mp3 bitrate in kbps. 64 is plenty for speech.                                                                  |
| `--overwrite`    | off                        | Regenerate audio for notes that already have some.                                                             |
| `--dry-run`      | off                        | Sync down and list what would change; upload nothing.                                                          |
| `--limit N`      | —                          | Only process the first N notes.                                                                                |
| `--concurrency`  | `2`                        | Parallel requests to the engine.                                                                               |
| `--wait`         | `60`                       | Seconds to wait for the engine to start.                                                                       |
| `--sync-url`     | —                          | Sync server URL. Env: `ANKI_SYNC_URL`.                                                                         |
| `--sync-user`    | —                          | Sync username. Env: `ANKI_USERNAME`. The password is only read from `ANKI_PASSWORD`.                           |
| `--data-dir`     | `data` (`/data` in Docker) | Where the local collection copy lives. Env: `anki-audio-syncer_DATA`.                                          |
| `--voicevox-url` | `http://127.0.0.1:50021`   | Engine address. Env: `VOICEVOX_URL`.                                                                           |

## Notes

- **Safe to re-run.** Notes whose audio field already has a sound are skipped,
  so new cards can be filled in later by running the same command.
- **What gets read.** HTML, existing `[sound:]` tags, and furigana readings
  (`漢字[かんじ]` → `漢字`) are stripped before synthesis.
- **Overwriting.** `--overwrite` replaces every `[sound:]` tag in the audio
  field. Without it, only audio this script added earlier (`anki-audio-syncer_*.mp3`) is
  ever replaced. Old mp3s stay in the media folder; Tools → Check Media in an
  Anki app removes unused ones.
- **Changing voice or speed** produces new file names, so use `--overwrite` to
  re-voice notes that already have audio.
- **Anki version.** The client is pinned to `anki==26.9.3` in
  `requirements.txt`. If your sync server rejects it as too new or too old,
  change the pin to the Anki version your server runs, then rebuild.
# anki-audio-syncer
# anki-audio-syncer
