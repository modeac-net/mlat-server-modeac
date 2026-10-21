# mlat-server with Mode A/C multilateration (modeac.net)
FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends gcc libc6-dev \
 && rm -rf /var/lib/apt/lists/*

# python-graph-core's setup.py no longer builds with current setuptools; it's pure Python,
# so install its package directory directly. Its __init__ files declare namespaces through
# pkg_resources, which setuptools >= 81 no longer ships, so drop those lines.
RUN pip install --no-cache-dir numpy scipy pykalman uvloop ujson Cython setuptools \
 && pip download --no-deps --no-binary :all: python-graph-core==1.8.2 -d /tmp/pg \
 && tar xzf /tmp/pg/python-graph-core-1.8.2.tar.gz -C /tmp/pg \
 && SP="$(python -c 'import site; print(site.getsitepackages()[0])')" \
 && cp -r /tmp/pg/python-graph-core-1.8.2/pygraph "$SP/" \
 && grep -rl --include=__init__.py pkg_resources "$SP/pygraph" | xargs -r sed -i '/pkg_resources/d' \
 && python -c "import pygraph.classes.graph, pygraph.algorithms.minmax" \
 && rm -rf /tmp/pg

WORKDIR /app
COPY . .
RUN python setup.py build_ext --inplace \
 && python -m unittest discover -s test -p "modeac_test.py" -t test

# which receiver locations are accepted: us | ohio | off | path to an outline JSON
ENV MODEAC_RECEIVER_AREA=us
ENV MKL_NUM_THREADS=1 OMP_NUM_THREADS=1 NUMEXPR_NUM_THREADS=1 PYTHONUNBUFFERED=1
EXPOSE 31090/tcp 31091/udp
VOLUME /run/mlat
ENTRYPOINT ["python3", "/app/mlat-server"]
CMD ["--client-listen", "31090:31091", "--work-dir", "/run/mlat", \
     "--modeac", "--modeac-fix-connect", "acfilter:39100", \
     "--modeac-pairs-log", "/run/mlat/pairs.jsonl", \
     "--modeac-client-filter", "/etc/modeac/client-filter.json", \
     "--banlist", "/etc/modeac/banlist.txt,/run/feedercheck/autoban.txt", \
     "--filtered-basestation-connect", "hub:32007"]
