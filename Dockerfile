FROM mwader/static-ffmpeg:7.0.2 AS ffmpeg
FROM python:3.12-slim

COPY --from=ffmpeg /ffmpeg /usr/local/bin/ffmpeg
COPY --from=ffmpeg /ffprobe /usr/local/bin/ffprobe

# Install Node.js (needed for the pot-token server)
RUN apt-get update && apt-get install -y curl unzip && \
    curl -fsSL https://deb.nodesource.com/setup_20.x | bash - && \
    apt-get install -y nodejs && \
    rm -rf /var/lib/apt/lists/*

# Install Deno (needed by yt-dlp to decipher YouTube's signature for high-quality formats)
RUN curl -fsSL https://deno.land/install.sh | sh
ENV PATH="/root/.deno/bin:${PATH}"

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Build the pot-server
RUN cd pot-server && npm ci && npx tsc

EXPOSE 8000
CMD bash start.sh
