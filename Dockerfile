FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Enable contrib repo for ttf-mscorefonts-installer (Calibri substitute etc.)
RUN sed -i 's/Components: main$/Components: main contrib non-free/' \
        /etc/apt/sources.list.d/debian.sources 2>/dev/null || \
    sed -i 's/ main$/ main contrib non-free/' /etc/apt/sources.list 2>/dev/null || true

# Accept Microsoft fonts EULA non-interactively
RUN echo "ttf-mscorefonts-installer msttcorefonts/accepted-mscorefonts-eula select true" \
    | debconf-set-selections

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    postgresql-client \
    fonts-dejavu-core \
    fonts-liberation \
    fonts-open-sans \
    ttf-mscorefonts-installer \
    fontconfig \
    git \
    gettext \
    libreoffice-writer \
    && fc-cache -f -v \
    && rm -rf /var/lib/apt/lists/* \
    && git config --global --add safe.directory /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Calibri fonts (proprietary — not committed to git, must be present in docker_fonts/)
COPY docker_fonts/ /usr/share/fonts/truetype/calibri/
RUN fc-cache -f -v

COPY . /app
