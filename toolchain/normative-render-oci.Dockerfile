FROM ghcr.io/quarto-dev/quarto@sha256:cdc12093a8fd224993a9e4643dd479f23727d7618031e84b3884065e0f9160f1

ARG DEBIAN_FRONTEND=noninteractive
ENV LANG=C LC_ALL=C TZ=UTC SOURCE_DATE_EPOCH=0 \
    PUPPETEER_SKIP_DOWNLOAD=true \
    PUPPETEER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates xz-utils unzip=6.0-26ubuntu3.2 \
      fonts-liberation libasound2 libatk-bridge2.0-0 libatk1.0-0 libcups2 \
      libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 libnspr4 libnss3 libx11-xcb1 \
      libxcomposite1 libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 xdg-utils \
      graphviz=2.42.2-6ubuntu0.1 \
      librsvg2-bin=2.52.5+dfsg-3ubuntu0.2 \
      locales=2.35-0ubuntu3.14 \
      poppler-utils=22.02.0-2ubuntu0.13 \
      qpdf=10.6.3-1ubuntu0.1 \
      tesseract-ocr=4.1.1-2.1build1 \
      tesseract-ocr-deu=1:4.00~git30-7274cfa-1.1 \
      texlive-fonts-recommended=2021.20220204-1 \
      texlive-lang-german=2021.20220204-1 \
      texlive-latex-extra=2021.20220204-1 \
      texlive-luatex=2021.20220204-1 \
 && apt-get clean \
 && rm -rf /var/lib/apt/lists/*

# Expose only the lock-authorized German OCR model to normative runs.
RUN mkdir -p /opt/brandkit-tessdata \
 && cp /usr/share/tesseract-ocr/4.00/tessdata/deu.traineddata /opt/brandkit-tessdata/deu.traineddata \
 && echo '19d219bbb6672c869d20a9636c6816a81eb9a71796cb93ebe0cb1530e2cdb22d  /opt/brandkit-tessdata/deu.traineddata' | sha256sum --check --strict
ENV TESSDATA_PREFIX=/opt/brandkit-tessdata

COPY external-artifacts/nodejs/c1bfeecf1d7404fa74728f9db72e697decbd8119ccc6f5a294d795756dfcfca7/node.tar.xz /tmp/node.tar.xz
COPY external-artifacts/chrome-for-testing/fba7d5e87cdcf0dc974c9ac8a0a9dbcf5686c935455d5e6beed337c6ada88bc2/chrome-linux64.zip /tmp/chrome.zip
RUN echo 'c1bfeecf1d7404fa74728f9db72e697decbd8119ccc6f5a294d795756dfcfca7  /tmp/node.tar.xz' | sha256sum --check --strict \
 && echo 'fba7d5e87cdcf0dc974c9ac8a0a9dbcf5686c935455d5e6beed337c6ada88bc2  /tmp/chrome.zip' | sha256sum --check --strict \
 && tar -xJf /tmp/node.tar.xz --strip-components=1 -C /usr/local \
 && unzip -q /tmp/chrome.zip -d /opt \
 && chown root:root /opt/chrome-linux64/chrome_sandbox \
 && chmod 4755 /opt/chrome-linux64/chrome_sandbox \
 && ln -s /opt/chrome-linux64/chrome /usr/local/bin/google-chrome-stable \
 && rm /tmp/node.tar.xz /tmp/chrome.zip

# Bind Puppeteer to the immutable Chrome-for-Testing executable installed above.
ENV PUPPETEER_EXECUTABLE_PATH=/usr/local/bin/google-chrome-stable \
    CHROME_DEVEL_SANDBOX=/opt/chrome-linux64/chrome_sandbox

WORKDIR /opt/brandkit-mermaid
COPY code/ai-brand-kit/toolchain/mermaid-svg/package.json code/ai-brand-kit/toolchain/mermaid-svg/package-lock.json ./
RUN npm ci --ignore-scripts --no-audit --no-fund \
 && npm cache clean --force

COPY code/ai-brand-kit/toolchain/pdf-renderer/fonts/abel-8809dcad25318225052f88333e208c5aad4adcb7b2c934c135735ec19aa410b4.ttf /usr/local/share/fonts/brandkit/Abel-Regular.ttf
RUN fc-cache -f \
 && quarto --version \
 && node --version \
 && google-chrome-stable --version \
 && /opt/brandkit-mermaid/node_modules/.bin/mmdc --version \
 && dot -V \
 && lualatex --version \
 && pdftoppm -v \
 && qpdf --version \
 && tesseract --version \
 && tesseract --list-langs | grep -Fx deu

WORKDIR /work
