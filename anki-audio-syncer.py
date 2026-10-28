#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["anki==26.9.3", "lameenc>=1.8,<2"]
# ///
"""Add Japanese text-to-speech audio to every note in an Anki deck.

Syncs the collection down from a self-hosted Anki sync server, reads the
source field (default: Front) of each note, synthesises it with a locally
running VOICEVOX-compatible engine, encodes it to mp3, stores it as Anki media
and writes a [sound:...] tag into the audio field (default: Audio), then syncs
the changes and the new audio back up. No Anki desktop app is needed.
"""

import argparse
import hashlib
import html
import io
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import wave
from collections import Counter
from concurrent.futures import ThreadPoolExecutor

import lameenc
from anki.collection import Collection

SOUND_TAG = re.compile(r"\[sound:[^\]]*\]")
OUR_SOUND_TAG = re.compile(r"\[sound:anki-audio-syncer_[^\]]*\]")
HTML_TAG = re.compile(r"<[^>]+>")
FURIGANA = re.compile(r"\[[^\]]*\]")  # 漢字[かんじ] style readings


class ServiceError(Exception):
    pass


# --- Anki sync -------------------------------------------------------------


def sync_login(col, args):
    endpoint = args.sync_url.rstrip("/") + "/"
    try:
        return col.sync_login(args.sync_user, args.sync_password, endpoint)
    except Exception as e:
        raise ServiceError(f"Logging in to the sync server at {endpoint} failed: {e}") from e


def sync_collection(col, auth, allow_full_download):
    """Normal sync. The local copy is only a cache, so when Anki demands a full
    sync we always take the server's version, and never upload a whole collection."""
    try:
        out = col.sync_collection(auth, sync_media=False)
    except Exception as e:
        raise ServiceError(f"Sync failed: {e}") from e
    if out.new_endpoint:
        auth.endpoint = out.new_endpoint
    if out.server_message:
        print(f"Sync server says: {out.server_message}")
    if out.required == out.NO_CHANGES:
        return

    if out.required == out.FULL_UPLOAD:
        raise ServiceError(
            "The sync server has no collection to download. Sync from an Anki app "
            "first; anki-audio-syncer never uploads a whole collection."
        )
    if not allow_full_download:
        raise ServiceError(
            "The server now requires a full sync (did a note type change mid-run?). "
            "Nothing was uploaded; run anki-audio-syncer again."
        )
    print("Downloading the full collection from the sync server ...")
    col.close_for_full_sync()
    try:
        col.full_upload_or_download(auth=auth, server_usn=None, upload=False)
    finally:
        col.reopen(after_full_sync=True)


def sync_media(col, auth):
    """Upload new audio (and download any media the cache is missing).

    col.sync_media() only starts a background task, and closing the collection
    aborts it, so poll until it finishes, like the desktop app does."""
    print("Syncing media ...")
    try:
        col.sync_media(auth)
        last = None
        while True:
            status = col.media_sync_status()  # raises if the sync failed
            p = status.progress
            line = " / ".join(s for s in (p.checked, p.added, p.removed) if s)
            if line and line != last:
                print(f"  {line}")
                last = line
            if not status.active:
                break
            time.sleep(1)
    except Exception as e:
        raise ServiceError(f"Media sync failed: {e}") from e
    print("Media sync complete.")


# --- VOICEVOX --------------------------------------------------------------


def voicevox(base_url, path, method="GET", body=None, **params):
    url = base_url.rstrip("/") + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data, {"Content-Type": "application/json"}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:200]
        raise ServiceError(f"VOICEVOX {path}: HTTP {e.code} {detail}") from e
    except OSError as e:
        raise ServiceError(
            f"Could not reach the VOICEVOX engine at {base_url} ({e}). Is it running?"
        ) from e


def talk_styles(base_url):
    """[(style_id, "Speaker (Style)")] for every speaking style the engine offers."""
    styles = []
    for speaker in json.loads(voicevox(base_url, "/speakers")):
        for style in speaker["styles"]:
            if style.get("type", "talk") == "talk":
                styles.append((style["id"], f"{speaker['name']} ({style['name']})"))
    return styles


def wait_for_engine(base_url, timeout):
    """Return the engine's talk styles, retrying while it is still starting up."""
    deadline = time.monotonic() + timeout
    waiting = False
    while True:
        try:
            return talk_styles(base_url)
        except ServiceError as e:
            if isinstance(e.__cause__, urllib.error.HTTPError) or time.monotonic() >= deadline:
                raise
            if not waiting:
                print(f"Waiting for the VOICEVOX engine at {base_url} ...")
                waiting = True
            time.sleep(2)


def resolve_speaker(args, styles):
    if not styles:
        raise ServiceError("The VOICEVOX engine reports no voices. Are its models installed?")
    if args.speaker is None:
        args.speaker, label = styles[0]
        print(f"No --speaker given, using {args.speaker}: {label} (see --list-voices)")
    elif args.speaker not in dict(styles):
        raise ServiceError(f"Speaker {args.speaker} not found. Run with --list-voices.")


def wav_to_mp3(wav_bytes, bitrate):
    with wave.open(io.BytesIO(wav_bytes)) as w:
        if w.getsampwidth() != 2:
            raise ServiceError(f"expected 16-bit WAV from engine, got {8 * w.getsampwidth()}-bit")
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(bitrate)
        encoder.set_in_sample_rate(w.getframerate())
        encoder.set_channels(w.getnchannels())
        encoder.set_quality(2)
        pcm = w.readframes(w.getnframes())
    return bytes(encoder.encode(pcm) + encoder.flush())


def synthesise(text, args):
    query = json.loads(
        voicevox(args.voicevox_url, "/audio_query", "POST", text=text, speaker=args.speaker)
    )
    query["speedScale"] = args.speed
    wav = voicevox(args.voicevox_url, "/synthesis", "POST", body=query, speaker=args.speaker)
    return wav_to_mp3(wav, args.bitrate)


# --- Notes -----------------------------------------------------------------


def clean_text(field_html):
    """Turn a field's HTML into plain text suitable for TTS."""
    text = SOUND_TAG.sub("", field_html)
    text = re.sub(r"<br\s*/?>|</div>", "\n", text, flags=re.I)
    text = HTML_TAG.sub("", text)
    text = html.unescape(text)
    text = FURIGANA.sub("", text)
    # Furigana syntax uses spaces as separators; Japanese doesn't need them.
    text = re.sub(r"[ 　]+", "", text)
    return text.strip()


def plan(col, args, stats):
    """Return [(note, text)] that need audio, counting the rest in stats."""
    query = args.query or f'deck:"{args.deck}"'
    note_ids = col.find_notes(query)
    if args.limit:
        note_ids = note_ids[: args.limit]
    print(f"Found {len(note_ids)} notes for {query!r}")

    jobs = []
    missing_models = set()
    for nid in note_ids:
        note = col.get_note(nid)
        if args.source_field not in note or args.audio_field not in note:
            missing_models.add(note.note_type()["name"])
            stats["missing"] += 1
        elif SOUND_TAG.search(note[args.audio_field]) and not args.overwrite:
            stats["skipped"] += 1
        elif not (text := clean_text(note[args.source_field])):
            stats["empty"] += 1
        else:
            jobs.append((note, text))

    if missing_models:
        print(
            f"{stats['missing']} notes lack a '{args.source_field}' or '{args.audio_field}' "
            f"field (note types: {', '.join(sorted(missing_models))}). Add the field in an "
            f"Anki app (Tools > Manage Note Types > Fields) and sync, or pass --audio-field Back."
        )
    return jobs


def add_audio(col, args, jobs, stats):
    with ThreadPoolExecutor(args.concurrency) as pool:
        pending = [(note, text, pool.submit(synthesise, text, args)) for note, text in jobs]
        for i, (note, text, future) in enumerate(pending, 1):
            try:
                audio = future.result()
            except Exception as e:
                print(f"  ! [{i}/{len(jobs)}] {text!r}: {e}", file=sys.stderr)
                stats["failed"] += 1
                continue

            key = hashlib.sha1(f"{args.speaker}|{args.speed}|{text}".encode()).hexdigest()[:16]
            filename = col.media.write_data(f"anki-audio-syncer_{key}.mp3", audio)
            # Replace any audio we added on a previous run, keep everything else.
            current = note[args.audio_field]
            kept = SOUND_TAG.sub("", current) if args.overwrite else OUR_SOUND_TAG.sub("", current)
            note[args.audio_field] = f"{kept}[sound:{filename}]"
            col.update_note(note)
            stats["done"] += 1
            print(f"  + [{i}/{len(jobs)}] {text}")


# --- Main ------------------------------------------------------------------


def run(args):
    styles = wait_for_engine(args.voicevox_url, args.wait)
    if args.list_voices:
        for style_id, label in styles:
            print(f"{style_id:>6}  {label}")
        return 0
    resolve_speaker(args, styles)

    os.makedirs(args.data_dir, exist_ok=True)
    col = Collection(os.path.join(args.data_dir, "collection.anki2"))
    try:
        auth = sync_login(col, args)
        sync_collection(col, auth, allow_full_download=True)

        stats = Counter()
        jobs = plan(col, args, stats)
        if args.dry_run:
            for note, text in jobs:
                print(f"[dry-run] {note.id}: {text!r}")
            print(f"\nDry run: would add audio to {len(jobs)} notes. Nothing was uploaded.")
            return 0

        add_audio(col, args, jobs, stats)
        if stats["done"]:
            print("Uploading changes to the sync server ...")
            sync_collection(col, auth, allow_full_download=False)
        # Always, so audio left over from an interrupted run still gets uploaded.
        sync_media(col, auth)
    finally:
        col.close()

    print(
        f"\nDone: added audio to {stats['done']}, "
        f"skipped {stats['skipped']} (already had audio), "
        f"{stats['empty']} empty, {stats['missing']} missing fields, {stats['failed']} failed."
    )
    return 1 if stats["failed"] else 0


def main():
    env = os.environ.get
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = p.add_mutually_exclusive_group()
    target.add_argument("--deck", help="deck name (includes subdecks)")
    target.add_argument("--query", help="raw Anki search query instead of --deck")
    p.add_argument("--list-voices", action="store_true",
                   help="list the engine's voices (style ids for --speaker) and exit")
    p.add_argument("--source-field", default="Front", help="field to read aloud (default: Front)")
    p.add_argument("--audio-field", default="Audio",
                   help="field to put the [sound:] tag in (default: Audio)")
    p.add_argument("--speaker", type=int,
                   default=int(env("VOICEVOX_SPEAKER")) if env("VOICEVOX_SPEAKER") else None,
                   help="VOICEVOX style id (env: VOICEVOX_SPEAKER; default: the engine's first voice)")
    p.add_argument("--speed", type=float, default=1.0, help="speech speed, e.g. 0.85 for slower")
    p.add_argument("--bitrate", type=int, default=64, help="mp3 bitrate in kbps (default: 64)")
    p.add_argument("--overwrite", action="store_true",
                   help="regenerate even if the audio field already has a sound")
    p.add_argument("--dry-run", action="store_true",
                   help="sync down and show what would happen; upload nothing")
    p.add_argument("--limit", type=int, help="only process the first N notes (for testing)")
    p.add_argument("--concurrency", type=int, default=2, help="parallel TTS requests (default: 2)")
    p.add_argument("--wait", type=float, default=60,
                   help="seconds to wait for the engine to start (default: 60)")
    p.add_argument("--voicevox-url", default=env("VOICEVOX_URL", "http://127.0.0.1:50021"),
                   help="VOICEVOX engine URL (env: VOICEVOX_URL)")
    p.add_argument("--sync-url", default=env("ANKI_SYNC_URL"),
                   help="Anki sync server URL, e.g. http://192.168.1.10:27701 (env: ANKI_SYNC_URL)")
    p.add_argument("--sync-user", default=env("ANKI_USERNAME"),
                   help="sync server username (env: ANKI_USERNAME)")
    p.add_argument("--data-dir", default=env("anki-audio-syncer_DATA", "data"),
                   help="where the local copy of the collection is kept (env: anki-audio-syncer_DATA)")
    args = p.parse_args()
    args.sync_password = env("ANKI_PASSWORD")

    if not args.list_voices:
        if not (args.deck or args.query):
            p.error("one of --deck or --query is required")
        missing = [name for name, value in (("ANKI_SYNC_URL", args.sync_url),
                                            ("ANKI_USERNAME", args.sync_user),
                                            ("ANKI_PASSWORD", args.sync_password)) if not value]
        if missing:
            p.error(f"set {', '.join(missing)} (environment or .env)")

    try:
        sys.exit(run(args))
    except ServiceError as e:
        sys.exit(f"Error: {e}")


if __name__ == "__main__":
    main()
