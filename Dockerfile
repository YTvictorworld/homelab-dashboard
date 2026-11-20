# Homelab terminal dashboard: collector + web page
FROM python:3.13-alpine
# ping (iputils), ip neigh (iproute2) and lspci (pciutils) to discover devices and GPUs
RUN apk add --no-cache iputils iproute2 procps tzdata pciutils
WORKDIR /app
COPY collector.py ./
COPY web/ ./web/
ENV PYTHONUNBUFFERED=1
EXPOSE 8090
CMD ["python3", "-I", "collector.py"]
