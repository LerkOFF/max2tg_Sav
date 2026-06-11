FROM alpine:3.20 AS python-builder

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHON_VERSION=3.9.20
ENV LIBRESSL_VERSION=2.8.3
ENV LIBRESSL_BASE_URL=https://cdn.openbsd.org/pub/OpenBSD/LibreSSL
ENV PATH=/opt/python/bin:$PATH

RUN apk add --no-cache \
    build-base \
    bzip2-dev \
    ca-certificates \
    curl \
    libffi-dev \
    linux-headers \
    ncurses-dev \
    perl \
    readline-dev \
    sqlite-dev \
    tar \
    xz \
    xz-dev \
    zlib-dev

WORKDIR /tmp

RUN curl --fail --location --retry 5 --retry-all-errors --retry-delay 5 --continue-at - \
        "${LIBRESSL_BASE_URL}/libressl-${LIBRESSL_VERSION}.tar.gz" \
        -o libressl.tgz \
    && mkdir libressl-src \
    && tar -xzf libressl.tgz -C libressl-src --strip-components=1 \
    && cd libressl-src \
    && ./configure --prefix=/opt/libressl \
    && make -j2 \
    && make install \
    && rm -rf /tmp/libressl.tgz /tmp/libressl-src

RUN curl --fail --location --retry 5 --retry-all-errors --retry-delay 5 --continue-at - \
        "https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tgz" \
        -o python.tgz \
    && mkdir python-src \
    && tar -xzf python.tgz -C python-src --strip-components=1 \
    && cd python-src \
    && CPPFLAGS="-I/opt/libressl/include" \
       LDFLAGS="-L/opt/libressl/lib -Wl,-rpath,/opt/libressl/lib" \
       ./configure --prefix=/opt/python --with-openssl=/opt/libressl \
    && make -j2 \
    && make install \
    && /opt/python/bin/python3 -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && rm -rf /tmp/python.tgz /tmp/python-src

RUN /opt/python/bin/python3 - <<'PY'
import ssl
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
ctx.set_ciphers("GOST2012256-GOST89-GOST89:GOST2001-GOST89-GOST89")
print(ssl.OPENSSL_VERSION)
PY

RUN /opt/python/bin/pip3 install --no-cache-dir \
    aiogram \
    aiosqlite \
    python-dotenv \
    lz4 \
    msgpack

FROM alpine:3.20

ENV PYTHONUNBUFFERED=1
ENV PYTHONDONTWRITEBYTECODE=1
ENV MAXAPI_REPO_PATH=/opt/maxapi
ENV LD_LIBRARY_PATH=/opt/libressl/lib
ENV PATH=/opt/python/bin:$PATH

RUN apk add --no-cache \
    bzip2 \
    ca-certificates \
    libffi \
    ncurses-libs \
    readline \
    sqlite-libs \
    xz-libs \
    zlib

WORKDIR /app

COPY --from=python-builder /opt/libressl /opt/libressl
COPY --from=python-builder /opt/python /opt/python
COPY vendor/MaxAPI /opt/maxapi
COPY . /app

RUN mkdir -p /app/data

CMD ["/opt/python/bin/python3", "-u", "main.py"]
