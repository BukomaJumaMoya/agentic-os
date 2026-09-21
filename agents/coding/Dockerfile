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
# WHY NOT node:22
# ---------------
# The full image is around 1.1 GB, almost all of it build toolchains this never
# uses. The slim image plus git is roughly a quarter of that, and git is the
# one thing beyond node that actually gets used -- Pi inspects the repository
# it is working in.
#
# The host still owns every git operation that matters. Branch creation and the
# diff that decides what "changed files" means both run on the host, against
# the real repository, after the container exits. Pi's opinion of what it
# changed is never the source of that answer.
FROM node:22-bookworm-slim

# git: Pi inspects the repository it works in.
# python3: the first end-to-end task ("create hello.py and run it") created the
# file and then failed with exit 127, because a node-only image cannot run
# Python. Most projects under the development root are Python, so an image
# without it can write code it can never execute -- and a coding agent that
# cannot run what it wrote cannot check its own work.
# python-is-python3: the agent reaches for `python` as often as `python3`.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        git ca-certificates python3 python3-venv python-is-python3 \
    && rm -rf /var/lib/apt/lists/*

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

WORKDIR /work
