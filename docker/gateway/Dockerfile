# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Switchyard source and plugin are pinned together. Keep the archive checksum
# and revision in sync when upgrading. Upstream sources retain Apache-2.0.
FROM public.ecr.aws/docker/library/rust:1.99.1-slim-bookworm@sha256:24707e70a4f1e23d9a7b25202ec43126d28a52294b96aad5c670df1fd1f1d400 AS switchyard-builder
RUN apt-get update \
    && apt-get install --yes --no-install-recommends patchelf python3 python3-pip python3-venv \
    && rm -rf /var/lib/apt/lists/*
RUN python3 -m venv /opt/maturin \
    && /opt/maturin/bin/pip install --no-cache-dir maturin==1.14.1
ADD --checksum=sha256:5411731fba92478363a61927cb26163babec77bf2fdb4f3d70b3f2c048fb3c51 https://codeload.github.com/NVIDIA-NeMo/Switchyard/tar.gz/21075c6652152dac88a5fbc6b878ca37d7617c1c /tmp/switchyard.tar.gz
WORKDIR /src
# The upstream Cargo.lock pins crates that have published advisories. Move each
# one to its first fixed release before the --locked build, and delete a line
# once the pinned Switchyard lockfile has that version or later. rustls 0.23.46
# needs aws-lc-rs 1.18 or later; pinning the aws-lc versions keeps rebuilds
# identical. PyO3 stays on 0.28 because 0.29 needs upstream manifest changes.
RUN tar -xzf /tmp/switchyard.tar.gz --strip-components=1 \
    && cargo update --package rustls --precise 0.23.46 \
    && cargo update --package aws-lc-rs --precise 1.19.0 \
    && cargo update --package aws-lc-sys --precise 0.47.0 \
    && cargo update --package rustls-webpki --precise 0.103.16 \
    && cargo update --package quinn-proto --precise 0.11.18 \
    && cargo update --package h2 --precise 0.4.16 \
    && cargo update --package webpki-root-certs --precise 1.0.10 \
    && /opt/maturin/bin/maturin build --release --locked \
       --compatibility manylinux_2_34 --out /wheels --interpreter /usr/bin/python3
# uv comes from PyPI, checked against the hashes in requirements-uv.txt.
COPY requirements-uv.txt /tmp/requirements-uv.txt
RUN /opt/maturin/bin/pip install --no-cache-dir --require-hashes --only-binary :all: \
    -r /tmp/requirements-uv.txt

FROM ghcr.io/berriai/litellm:v1.102.2@sha256:5970a1a871d84a52bfae97bd723ab0dcfe3d8ee5614037bc9f951e9f5faa8263
# Remove the openssl command-line package first. Nothing in the image uses it,
# and its configuration files block the python-3.13 release that fixes
# CVE-2026-115557, which links OpenSSL 4.0. Then upgrade only the base image
# packages that have CVE fixes; a full `apk upgrade` would replace the rest of
# the base as well. The glibc-2.44 packages require each other at the same
# release, so each one gets the same minimum, and python-3.13 pins
# python-3.13-base at its own version, so one floor moves both. Then remove
# Node.js (the base image's `nodejs` world entry, which installs nodejs-26).
# Only LiteLLM's Prisma database CLI uses it, and this gateway configures no
# database.
RUN apk del --no-cache openssl \
    && apk add --no-cache --upgrade 'libbrotlicommon1>=1.2.0-r7' 'libbrotlidec1>=1.2.0-r7' \
    'libbrotlienc1>=1.2.0-r7' 'ca-certificates-bundle>=20260909-r3' 'py3-pip-wheel>=26.2.1-r2' \
    'glibc-2.44>=2.44-r4' 'glibc-2.44-locale-posix>=2.44-r4' 'ld-linux-2.44>=2.44-r4' \
    'libcrypt1-2.44>=2.44-r4' 'zlib>=1.3.2.1_rc20260601-r0' \
    'python-3.13>=3.13.18' 'libexpat1>=2.10.0' \
    && apk del --no-cache nodejs \
    && if command -v node; then echo 'Node.js is still installed' >&2; exit 1; fi
# uv is mounted only for the install steps, so it does not ship in the runtime image.
# Replace vulnerable Python packages with the hashed wheels in
# requirements-cve-patches.txt and remove the optional Datadog tracer, which
# LiteLLM imports only when USE_DDTRACE is set. Then fail the build on any
# `uv pip check` conflict except the base image's own langfuse/packaging one.
RUN --mount=type=bind,from=switchyard-builder,source=/opt/maturin/bin/uv,target=/usr/local/bin/uv \
    --mount=type=bind,source=requirements-cve-patches.txt,target=/tmp/requirements-cve-patches.txt \
    uv pip install --no-cache --python /app/.venv/bin/python --no-deps --require-hashes --no-build \
       -r /tmp/requirements-cve-patches.txt \
    && uv pip uninstall --python /app/.venv/bin/python ddtrace \
    && { uv pip check --python /app/.venv/bin/python > /tmp/pip-check.txt 2>&1 || true; } \
    && cat /tmp/pip-check.txt \
    && grep -Eq '^Checked [0-9]+ packages?' /tmp/pip-check.txt \
    && if grep '^The package' /tmp/pip-check.txt | grep -v '^The package `langfuse` requires `packaging'; then \
         echo 'uv pip check reported a conflict other than langfuse/packaging' >&2; exit 1; \
       fi \
    && rm /tmp/pip-check.txt
RUN --mount=type=bind,from=switchyard-builder,source=/opt/maturin/bin/uv,target=/usr/local/bin/uv \
    --mount=type=bind,from=switchyard-builder,source=/wheels,target=/wheels \
    uv pip install --no-cache --python /app/.venv/bin/python --no-deps /wheels/*.whl
COPY --from=switchyard-builder /src/examples/litellm/src /app/switchyard-plugin/src
COPY --from=switchyard-builder /src/LICENSE /src/NOTICE /usr/share/licenses/switchyard/
# The gateway wrapper routes multi-model groups with Switchyard and passes
# single-model baseline groups through unchanged.
COPY switchyard_gateway /app/switchyard-gateway/switchyard_gateway
ENV PYTHONPATH=/app/switchyard-plugin/src:/app/switchyard-gateway
# Use the cost map bundled in the pinned image so routing and model limits
# don't change with a remote file, on every run path.
ENV LITELLM_LOCAL_MODEL_COST_MAP=True
# Fail the build if the pieces do not import together on LiteLLM 1.102.2, the
# pinned patch release of the 1.102 line the Switchyard plugin targets.
RUN python -c "from importlib.metadata import version; from switchyard.libsy import algorithms; from switchyard_litellm import StageRoutingPlugin, SwitchyardRoutingPlugin; from switchyard_gateway import MultiCandidateRouting; assert version('litellm') == '1.102.2'; SwitchyardRoutingPlugin(algorithms.random()); MultiCandidateRouting(StageRoutingPlugin(picker='efficient_first', confidence_threshold=0.5))"
COPY --chmod=755 start.sh /app/gateway-start.sh
RUN addgroup -S -g 10001 gateway \
    && adduser -S -D -u 10001 -G gateway -h /home/gateway gateway
USER gateway
EXPOSE 4000
ENTRYPOINT ["/app/gateway-start.sh"]
