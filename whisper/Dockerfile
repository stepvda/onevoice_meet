# whisper.cpp server — self-hosted free transcription for completed
# recordings. Built from a pinned upstream tag so reproducible builds are
# possible. Final image ships the `whisper-server` binary plus the
# `ggml-base.en` model (~145 MB) baked in, so the container starts
# instantly without needing internet at runtime.
#
# Stage 1: build the server binary using cmake.
FROM debian:bookworm-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git ca-certificates curl bash && \
    rm -rf /var/lib/apt/lists/*

ARG WHISPER_TAG=v1.7.4
WORKDIR /src
RUN git clone --depth 1 --branch ${WHISPER_TAG} https://github.com/ggerganov/whisper.cpp .

# Build everything (release, multi-threaded). The exact target name has
# changed across whisper.cpp versions (`server` → `whisper-server`), so
# we build the whole tree and copy whatever's in build/bin/.
RUN cmake -B build -DCMAKE_BUILD_TYPE=Release && \
    cmake --build build --config Release -j$(nproc)

# Bake in base.en (English-only, ~145 MB). Other models (tiny/small/medium)
# can be swapped in later by replacing this download step + the MODEL env.
RUN bash ./models/download-ggml-model.sh base.en

# Stage 2: minimal runtime.
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        libstdc++6 ca-certificates && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /src/build/bin/ /usr/local/bin/
# whisper.cpp's cmake build produces shared libraries (libwhisper.so,
# libggml*.so) in scattered subdirectories of build/. Walk the entire tree
# and copy every .so* file into /usr/local/lib so the dynamic loader can
# find them via ldconfig — without us having to track exact paths that
# shift between releases.
COPY --from=builder /src/build /tmp/whisper-build
RUN find /tmp/whisper-build -name "*.so*" -exec cp -a {} /usr/local/lib/ \; && \
    rm -rf /tmp/whisper-build && \
    ldconfig
COPY --from=builder /src/models/ggml-base.en.bin /models/ggml-base.en.bin

ENV MODEL=/models/ggml-base.en.bin \
    THREADS=2 \
    PORT=8080

EXPOSE 8080

# whisper.cpp v1.7+ ships the binary as `whisper-server`; older versions
# called it `server`. Pick whichever is on $PATH.
CMD ["sh", "-c", "BIN=$(command -v whisper-server 2>/dev/null || command -v server) && exec $BIN -m $MODEL --host 0.0.0.0 --port $PORT --threads $THREADS"]
