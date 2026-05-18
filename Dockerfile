# agkit.io-topography Lambda image.
#
# Base: the official OSGeo GRASS GIS image (Alpine variant), pinned to a
# release tag. Replaces the long-stale mundialis/grass-py3-pdal:latest-alpine
# (Alpine 3.15 / Python 3.9 / GDAL 3.4 / numpy 1.x) — that tag had not been
# rebuilt in years and shipped GDAL bindings compiled against numpy 1.x, so
# any fresh build broke gdal_array under numpy 2.
#
# osgeo/grass-gis:8.5.0-alpine — Alpine 3.23, Python 3.12, GDAL 3.11, GRASS
# 8.5, numpy 2.3. numpy and pillow are provided as system packages
# (py3-numpy, py3-pillow), built against this image's GDAL.
ARG FUNCTION_DIR="/code"

FROM osgeo/grass-gis:8.5.0-alpine AS base

# ---------------------------------------------------------------------------
# Build stage — compile awslambdaric (the AWS Lambda Runtime Interface
# Client) and vendor the pure-Python deps into FUNCTION_DIR.
# ---------------------------------------------------------------------------
FROM base AS build-image
ARG FUNCTION_DIR

RUN apk add --no-cache \
    build-base \
    python3-dev \
    cmake \
    autoconf \
    automake \
    libtool \
    curl-dev \
    pkgconf \
    py3-pip

# awslambdaric bundles aws-lambda-cpp, which #include's <execinfo.h>. Alpine
# dropped libexecinfo with 3.17, so provide a no-op stub — execinfo only
# backs C++ crash backtraces, irrelevant under Lambda (CloudWatch already
# captures the failure and stack).
RUN printf '%s\n' \
    '#ifndef _EXECINFO_H' \
    '#define _EXECINFO_H' \
    '#ifdef __cplusplus' \
    'extern "C" {' \
    '#endif' \
    'static inline int backtrace(void **b, int s) { (void)b; (void)s; return 0; }' \
    'static inline char **backtrace_symbols(void *const *b, int s) { (void)b; (void)s; return 0; }' \
    'static inline void backtrace_symbols_fd(void *const *b, int s, int fd) { (void)b; (void)s; (void)fd; }' \
    '#ifdef __cplusplus' \
    '}' \
    '#endif' \
    '#endif' > /usr/include/execinfo.h

COPY requirements.txt /tmp/requirements.txt
RUN mkdir -p ${FUNCTION_DIR} \
    && pip3 install --no-cache-dir --break-system-packages \
        --target ${FUNCTION_DIR} awslambdaric -r /tmp/requirements.txt

# ---------------------------------------------------------------------------
# Runtime image.
# ---------------------------------------------------------------------------
FROM base
ARG FUNCTION_DIR
WORKDIR ${FUNCTION_DIR}

# awslambdaric + vendored pure-Python deps. numpy/pillow/gdal come from the
# base image's system site-packages.
COPY --from=build-image ${FUNCTION_DIR} ${FUNCTION_DIR}

# Lambda Runtime Interface Emulator — only used for local `docker run`.
ADD https://github.com/aws/aws-lambda-runtime-interface-emulator/releases/latest/download/aws-lambda-rie /usr/bin/aws-lambda-rie
COPY entry.sh /
RUN chmod 755 /usr/bin/aws-lambda-rie /entry.sh

RUN mkdir -p app
COPY app/* app/

# GISBASE and the GRASS python path are already exported by the base image;
# grass_handler.initialize_grassdb() resolves and sets the rest itself.
# Prepend FUNCTION_DIR so `app.*` and awslambdaric resolve; HOME=/tmp keeps
# GRASS scratch on the only writable Lambda mount.
ENV PYTHONPATH="/code:/usr/local/grass/etc/python" \
    HOME="/tmp"

ENTRYPOINT [ "/entry.sh" ]
CMD [ "app.handler.handler" ]
