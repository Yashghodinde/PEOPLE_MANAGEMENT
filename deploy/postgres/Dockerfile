FROM postgres:16
RUN apt-get update && apt-get install -y --no-install-recommends age zstd && rm -rf /var/lib/apt/lists/*
COPY deploy/postgres/postgresql.conf /etc/postgresql/postgresql.conf
COPY deploy/postgres/ots-wal-archive.sh /usr/local/bin/ots-wal-archive.sh
RUN chmod +x /usr/local/bin/ots-wal-archive.sh
CMD ["postgres", "-c", "config_file=/etc/postgresql/postgresql.conf"]
