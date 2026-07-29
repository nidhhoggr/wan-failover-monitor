# wan-failover-monitor
#
# Small alpine image that runs monitor.py in a loop. We pull in iputils-ping
# because alpine's busybox ping doesn't reliably support -i (interval) and
# some -W (timeout) semantics we rely on for parallel probing; iputils gives
# us consistent, parseable output across the option set we use.

FROM python:3.12-alpine

RUN apk add --no-cache iputils curl tzdata

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY omada_client.py monitor.py db.py dashboard.py ./

# Run as non-root where possible. NOTE: raw ICMP via the `ping` binary needs
# either root or the CAP_NET_RAW capability. We shell out to the setuid/setcap
# `ping` binary rather than opening raw sockets from Python, which is why we
# don't need to run the whole container as root -- grant CAP_NET_RAW at
# `docker run`/compose level instead (see docker-compose.yml) and drop the
# rest of root's privileges here.
RUN adduser -D monitor && mkdir -p /data && chown monitor:monitor /data
USER monitor

# /data holds the shared sqlite db (monitor.py writes, dashboard.py reads).
# Mounted as a named volume in docker-compose.yml so it survives rebuilds.
VOLUME ["/data"]

CMD ["python", "-u", "monitor.py"]
