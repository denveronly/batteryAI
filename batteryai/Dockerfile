ARG BUILD_FROM
FROM ${BUILD_FROM}

# The Supervisor passes the add-on version from config.yaml; shown in the panel header.
ARG BUILD_VERSION=dev
ENV BATTERYAI_VERSION=${BUILD_VERSION}

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

# Local LLM engine: llama.cpp (via llama-cpp-python) compiled for this machine's CPU.
# The add-on is built on the Home Assistant host, so this takes a while on small CPUs
# (10-30 minutes on a Raspberry Pi). If the build fails, the add-on still installs and
# only the "Local slow" prediction engine is unavailable.
ARG LLAMA_CPP_PYTHON_VERSION=0.3.36
RUN apk add --no-cache libstdc++ libgomp \
    && apk add --no-cache --virtual .llama-build build-base cmake linux-headers \
    && ( CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)" CMAKE_ARGS="-DLLAMA_BUILD_SERVER=OFF -DLLAMA_BUILD_EXAMPLES=OFF -DLLAMA_BUILD_TESTS=OFF" \
         pip3 install --no-cache-dir "llama-cpp-python==${LLAMA_CPP_PYTHON_VERSION}" \
         || echo "WARNING: llama-cpp-python could not be built; the local LLM engine will be unavailable" ) \
    && apk del .llama-build

COPY app/ /app/

CMD ["python3", "-u", "/app/main.py"]
