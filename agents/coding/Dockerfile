# Sandbox for the Pi coding agent.
#
# WHY THIS EXISTS
# ---------------
# Pi ships with an unrestricted bash tool. That is a reasonable default for a
# developer running it on their own machine and an unreasonable one for a
# process driven by a model that has just read an instruction forwarded from
# Telegram. Inside a container, Pi's bash reaches the project directory and
# nothing else: not D:\, not the Hermes secret store, not the other agents'
# .env files, not the host's ssh keys.
#
# WHY PI IS INSTALLED HERE RATHER THAN MOUNTED
# --------------------------------------------
# It used to be bind-mounted read-only from agents/coding/vendor. It now lives
# in the image because both token-reduction tools have to be set up at BUILD
# time -- `pi install` writes into the agent user's home, and a read-only mount
# cannot be initialised. Installing Pi here also removes a mount and makes the
# sandbox reproducible from this file alone.
#
# The host still owns every git operation that matters. Branch creation and the
# diff that decides what "changed files" means both run on the host, against
# the real repository, after the container exits. Pi's opinion of what it
# changed is never the source of that answer.
#
# EVERY VERSION HERE IS PINNED
# ----------------------------
# Three pieces of third-party code run inside this sandbox with the model
# driving them. `latest` would mean the sandbox silently changes under a
# rebuild, which is exactly what you do not want of a confinement boundary.
FROM node:22-bookworm-slim

# git: Pi inspects the repository it works in.
# python3: a coding agent that cannot run what it wrote cannot check its own
# work, and most projects under the development root are Python. The first
# end-to-end task created hello.py and then failed with exit 127 on a node-only
# image.
# python-is-python3: the agent reaches for `python` as often as `python3`.
# curl/ca-certificates: fetching the pinned RTK release below.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates python3 python3-venv python-is-python3 curl \
    && rm -rf /var/lib/apt/lists/*

# ---------------------------------------------------------------------------
# RTK -- rewrites shell commands so their output costs fewer tokens.
#
# v0.49.0 is the newest NON-prerelease: every tag after it is a dev-0.50.0-rc.
# A release candidate is not a thing to pin a sandbox to.
#
# The tarball is verified against the checksums.txt published with the same
# tag. Downloading a binary into a security boundary without checking it is
# how the boundary stops being one.
# ---------------------------------------------------------------------------
ARG RTK_VERSION=v0.49.0
ARG RTK_ASSET=rtk-x86_64-unknown-linux-musl.tar.gz
# The asset keeps its published filename: `sha256sum -c` looks the name up on
# disk exactly as checksums.txt spells it, so renaming it on download makes the
# verification silently unrunnable rather than failing loudly.
RUN set -eux; \
    base="https://github.com/rtk-ai/rtk/releases/download/${RTK_VERSION}"; \
    cd /tmp; \
    curl -fsSL -O "${base}/${RTK_ASSET}"; \
    curl -fsSL -O "${base}/checksums.txt"; \
    grep " ${RTK_ASSET}\$" checksums.txt > want.txt; \
    sha256sum -c want.txt; \
    tar -xzf "${RTK_ASSET}" -C /tmp; \
    install -m 0755 "$(find /tmp -name rtk -type f | head -1)" /usr/local/bin/rtk; \
    rm -rf "/tmp/${RTK_ASSET}" /tmp/checksums.txt /tmp/want.txt; \
    rtk --version

# Telemetry is opt-in and off by default, but "off by default" is a decision
# someone can flip. This blocks it regardless of consent state, and it is set
# as an image ENV so it applies to every process in the container, not just the
# build.
ENV RTK_TELEMETRY_DISABLED=1

# ---------------------------------------------------------------------------
# Pi, installed from the SAME lockfile the host vendor directory uses.
#
# This was `npm install -g @earendil-works/pi-coding-agent@${PI_VERSION}`, and
# the comment above it claimed the image was "pinned to the same version the
# host vendor directory holds". It pinned the top-level version and nothing
# else: npm resolved 250-odd transitive packages fresh on every build, so two
# builds of this same Dockerfile a week apart were two different images.
#
# Copying package-lock.json in and running `npm ci` makes the claim true. `ci`
# fails outright when the lockfile and package.json disagree, which is the
# behaviour worth having here -- a mismatch should stop the build rather than
# be quietly resolved into something nobody audited.
#
# The version check afterwards is not belt-and-braces. PI_VERSION also feeds
# JUMA_SANDBOX_PI, which the coding agent reports as the version it ran under.
# If the lockfile moves and this ARG does not, that report becomes a lie.
# ---------------------------------------------------------------------------
ARG PI_VERSION=0.86.1
COPY vendor/package.json vendor/package-lock.json /opt/pi/
RUN cd /opt/pi \
    && npm ci \
    && installed="$(node -p "require('/opt/pi/node_modules/@earendil-works/pi-coding-agent/package.json').version")" \
    && { [ "$installed" = "${PI_VERSION}" ] \
         || { echo "lockfile has Pi $installed, PI_VERSION says ${PI_VERSION}"; exit 1; }; } \
    && ln -s /opt/pi/node_modules/.bin/pi /usr/local/bin/pi \
    && npm cache clean --force \
    && pi --version

# A non-root user, so files Pi creates in the mounted project are owned by a
# normal uid rather than by root. Root-owned files in a repo on a bind mount
# are a genuine nuisance to clean up afterwards.
RUN useradd --create-home --uid 1001 agent
USER agent

# git refuses to operate in a directory owned by a different user, which is
# exactly what a bind-mounted host repository looks like from inside.
RUN git config --global --add safe.directory '*' \
    && git config --global user.email "agent@localhost" \
    && git config --global user.name "pi-coding-agent"

# ---------------------------------------------------------------------------
# Wire both tools into Pi, as the agent user so the config lands in this
# image's /home/agent and NOT on any mounted path. That placement is the point:
# RTK's failure-recall store and Pi's package cache stay inside the container
# and die with it, so nothing the model does in one task persists into the
# next or leaks onto the host.
#
# Ponytail is pinned to a commit, not a branch: it is a ruleset injected into
# the model's context, so "whatever main says today" would change the agent's
# behaviour without any change here.
# ---------------------------------------------------------------------------
ARG PONYTAIL_COMMIT=e3ba2aa6f1e6f0bc4d69eb09c9f0d0a93af56156
RUN rtk init -g --agent pi \
    && pi install "git:github.com/DietrichGebert/ponytail@${PONYTAIL_COMMIT}" \
    && rtk telemetry status

# Recorded so a running container can state what it is, and so the coding agent
# can report the versions it actually ran under.
ENV JUMA_SANDBOX_RTK=${RTK_VERSION} \
    JUMA_SANDBOX_PI=${PI_VERSION} \
    JUMA_SANDBOX_PONYTAIL=${PONYTAIL_COMMIT}

WORKDIR /work
