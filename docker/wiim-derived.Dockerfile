# Derived Music Assistant Server image with WiiM remote transport enhancements
# This image applies a patch containing feature commits on top of the official base image.
#
# ARG BASE_VERSION: upstream image version to build on (automatically bumped by upstream-bump workflow)
# ARG BRANCH_BASE: git tag that this branch's commits are rebased onto (only changes on manual rebase)

ARG BASE_VERSION=2.10.1
ARG BRANCH_BASE=2.10.0
FROM ghcr.io/music-assistant/server:${BASE_VERSION}

COPY overlay.patch /tmp/overlay.patch

RUN set -eu; \
    apt-get update && apt-get install -y --no-install-recommends patch; \
    \
    matches="$(ls -d /app/venv/lib/python*/site-packages 2>/dev/null || true)"; \
    if [ -z "$matches" ]; then \
      echo "ERROR: no site-packages directory under /app/venv/lib/python*/"; \
      find /app/venv/lib -maxdepth 1 -type d -name 'python*' 2>/dev/null || echo "  (no python* dirs at all)"; \
      exit 1; \
    fi; \
    count="$(printf '%s\n' "$matches" | wc -l)"; \
    if [ "$count" -ne 1 ]; then \
      echo "ERROR: expected exactly one site-packages directory, found $count:"; \
      printf '%s\n' "$matches"; \
      exit 1; \
    fi; \
    site_packages="$matches"; \
    echo "Using site-packages directory: $site_packages"; \
    # Prevent .orig backups when hunks apply at an offset; .rej still indicates genuine failures.
    patch -d "$site_packages" --batch --forward --fuzz=0 --strip=1 --no-backup-if-mismatch < /tmp/overlay.patch || { \
      echo "ERROR: patch application failed"; \
      exit 1; \
    }; \
    find "$site_packages/music_assistant" -name '*.rej' -o -name '*.orig' | grep -q . && { \
      echo "ERROR: patch left behind .rej or .orig files:"; \
      find "$site_packages/music_assistant" \( -name '*.rej' -o -name '*.orig' \) -exec cat {} \;; \
      exit 1; \
    }; \
    rm -rf "$site_packages/music_assistant/models/__pycache__" \
           "$site_packages/music_assistant/controllers/streams/__pycache__" \
           "$site_packages/music_assistant/controllers/player_queues/__pycache__" \
           "$site_packages/music_assistant/controllers/metadata/__pycache__" \
           "$site_packages/music_assistant/providers/wiim/__pycache__"; \
    /app/venv/bin/python -m compileall -q "$site_packages/music_assistant/models" \
                                            "$site_packages/music_assistant/controllers" \
                                            "$site_packages/music_assistant/providers/wiim"; \
    grep -q 'def on_stream_requested' "$site_packages/music_assistant/models/player.py" || { \
      echo "ERROR: marker 'def on_stream_requested' not found in models/player.py"; \
      exit 1; \
    }; \
    grep -q 'on_stream_requested(' "$site_packages/music_assistant/controllers/streams/controller.py" || { \
      echo "ERROR: marker 'on_stream_requested(' not found in controllers/streams/controller.py"; \
      exit 1; \
    }; \
    grep -q '_resolve_remote_button_press' "$site_packages/music_assistant/providers/wiim/player.py" || { \
      echo "ERROR: marker '_resolve_remote_button_press' not found in providers/wiim/player.py"; \
      exit 1; \
    }; \
    grep -q '_last_skip_press' "$site_packages/music_assistant/controllers/player_queues/controller.py" || { \
      echo "ERROR: marker '_last_skip_press' not found in controllers/player_queues/controller.py"; \
      exit 1; \
    }; \
    grep -q 'online metadata is disabled: a refresh task would be a no-op' "$site_packages/music_assistant/controllers/metadata/controller.py" || { \
      echo "ERROR: marker 'online metadata is disabled: a refresh task would be a no-op' not found in controllers/metadata/controller.py"; \
      exit 1; \
    }; \
    apt-get purge -y patch && \
    apt-get autoremove -y && \
    rm -rf /var/lib/apt/lists/* /tmp/overlay.patch; \
    echo "✓ Successfully applied WiiM remote transport feature patch"
