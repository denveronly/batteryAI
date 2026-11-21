ARG BUILD_FROM
FROM ${BUILD_FROM}
RUN apk add --no-cache arp-scan \
 && pip install --no-cache-dir aiohttp==3.10.10
COPY app /app
CMD ["python3", "-u", "/app/main.py"]
