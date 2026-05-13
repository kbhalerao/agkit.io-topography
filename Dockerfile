# agkit.io-topography Lambda image.
# Carried over from USGS2021's DockerfileAlpineGrass (the known-good variant).
ARG FUNCTION_DIR="/code"

FROM mundialis/grass-py3-pdal:latest-alpine AS base
RUN apk add --no-cache libstdc++

FROM base AS build-image
RUN apk add --no-cache \
    build-base \
    libtool \
    autoconf \
    automake \
    libexecinfo-dev \
    python3-dev \
    make \
    cmake \
    libcurl

ARG FUNCTION_DIR
RUN mkdir -p ${FUNCTION_DIR}
RUN python -m pip install --upgrade pip
RUN python -m pip install --target ${FUNCTION_DIR} awslambdaric

FROM mundialis/grass-py3-pdal:latest-alpine
ARG FUNCTION_DIR
WORKDIR ${FUNCTION_DIR}

COPY --from=build-image ${FUNCTION_DIR} ${FUNCTION_DIR}
COPY requirements.txt /${FUNCTION_DIR}/requirements.txt
RUN pip3 install --no-cache-dir -r requirements.txt

# Lambda Runtime Interface Emulator for local runs.
ADD https://github.com/aws/aws-lambda-runtime-interface-emulator/releases/latest/download/aws-lambda-rie /usr/bin/aws-lambda-rie
COPY entry.sh /
RUN chmod 755 /usr/bin/aws-lambda-rie /entry.sh

RUN mkdir app
COPY app/* app/

ENV GRASSBIN="/usr/local/bin/grass" \
    PYTHONPATH="/code/" \
    LD_LIBRARY_PATH="/usr/local/grass/lib" \
    GISBASE="/usr/local/grass" \
    HOME="/tmp"
ENV PATH="$PATH:$GISBASE/bin:$GISBASE/scripts"

ENTRYPOINT [ "/entry.sh" ]
CMD [ "app.handler.handler" ]
