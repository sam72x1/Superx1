FROM python:3.11.9-slim-bookworm

ARG KRONOS_SOURCE_REVISION=67b630e67f6a18c9e9be918d9b4337c960db1e9a

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    KRONOS_REPO_PATH=/opt/kronos \
    KRONOS_SOURCE_REVISION=${KRONOS_SOURCE_REVISION} \
    KRONOS_HOST=0.0.0.0 \
    KRONOS_PORT=8080 \
    HF_HOME=/var/cache/huggingface

WORKDIR /app

RUN apt-get update \
    && apt-get install --yes --no-install-recommends ca-certificates git tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY services/kronos_inference/requirements.txt /tmp/kronos-requirements.txt
# هذه صورة CPU؛ ثبّت wheel الرسمي الخفيف أولًا. يطابق 2.13.0+cpu القيد
# torch==2.13.0 في الملف التالي، فلا يعيد pip تنزيل توزيعة CUDA العامة.
RUN python -m pip install --no-cache-dir "torch==2.13.0+cpu" \
        --index-url https://download.pytorch.org/whl/cpu \
    && python -m pip install --no-cache-dir -r /tmp/kronos-requirements.txt

RUN useradd --create-home --shell /usr/sbin/nologin kronos \
    && git clone https://github.com/shiyu-coder/Kronos.git /opt/kronos \
    && git -C /opt/kronos checkout --detach "${KRONOS_SOURCE_REVISION}" \
    && test "$(git -C /opt/kronos rev-parse HEAD)" = "${KRONOS_SOURCE_REVISION}" \
    && mkdir -p /var/cache/huggingface \
    && chown -R kronos:kronos /opt/kronos /var/cache/huggingface

COPY services /app/services

USER kronos

# اخبز المراجعات المثبتة في الصورة: النشر لا يعتمد على Hugging Face وقت
# الإقلاع، ويظل runtime قابلًا للعمل مع منع الشبكة إلى الـHub.
RUN python -c "from huggingface_hub import snapshot_download; snapshot_download('NeoQuasar/Kronos-Tokenizer-base', revision='0e0117387f39004a9016484a186a908917e22426'); snapshot_download('NeoQuasar/Kronos-small', revision='901c26c1332695a2a8f243eb2f37243a37bea320')"

ENV HF_HUB_OFFLINE=1

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; port=os.environ.get('KRONOS_PORT', '8080'); urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=3).read()"]

CMD ["python", "-m", "services.kronos_inference"]
