FROM ubuntu:26.04
RUN apt-get update && apt-get install -y --no-install-recommends supervisor python3 && rm -rf /var/lib/apt/lists/*
RUN mkdir -p /opt/conf /home/x/.run && useradd -m -u 1000 -s /bin/bash x && chown -R 1000:1000 /home/x
COPY _tmp_supervisor_test/supervisord.conf /opt/conf/supervisord.conf
CMD ["/bin/bash", "-c", "python3 -c 'import os; os.fchmod(1,0o666); os.fchmod(2,0o666)' && echo FCHMOD_DONE && exec /usr/bin/supervisord -n -c /opt/conf/supervisord.conf"]
