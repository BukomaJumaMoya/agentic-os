# The inbound boundary, containerised.
#
# It is NOT published to the host. n8n reaches it by container DNS on a private
# Docker network, so the port exists on no host interface at all -- not the
# Wi-Fi address, not even loopback. That is stronger than the "localhost-bound"
# the brief asked for, and it is the only shape that also works: a host
# loopback listener is unreachable from a container, which is what
# `connect ECONNREFUSED 127.0.0.1:8787` was telling us.
FROM python:3.12-alpine
RUN adduser -D -u 10001 receiver
WORKDIR /app
COPY receiver.py /app/receiver.py
USER receiver
ENV PYTHONUNBUFFERED=1
# /data holds .env (the shared secret) and queue.jsonl, bind-mounted from
# agents/n8n so the host-side drain script can read the queue.
ENV RECEIVER_DATA=/data
EXPOSE 8787
CMD ["python", "/app/receiver.py"]
