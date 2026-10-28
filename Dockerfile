FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    VOICEVOX_URL=http://host.docker.internal:50021 \
    anki-audio-syncer_DATA=/data

WORKDIR /app
# --chmod: source files may be unreadable to "other" (e.g. VirtualBox shared folders).
COPY --chmod=644 requirements.txt .
RUN pip install -r requirements.txt

COPY --chmod=644 anki-audio-syncer.py .

RUN useradd --create-home --uid 1000 anki-audio-syncer \
    && mkdir /data && chown anki-audio-syncer:anki-audio-syncer /data
USER anki-audio-syncer
VOLUME /data

ENTRYPOINT ["python", "anki-audio-syncer.py"]
CMD ["--help"]
