ARG BUILD_FROM
FROM ${BUILD_FROM}

ARG CHARTJS_VERSION=4.4.6

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip3 install --no-cache-dir -r /app/requirements.txt

COPY app/ /app/

# Bundle Chart.js so the UI works without internet access from the browser.
RUN mkdir -p /app/static/vendor \
    && curl -fsSL -o /app/static/vendor/chart.umd.min.js \
       "https://cdn.jsdelivr.net/npm/chart.js@${CHARTJS_VERSION}/dist/chart.umd.min.js"

CMD ["python3", "-u", "/app/main.py"]
