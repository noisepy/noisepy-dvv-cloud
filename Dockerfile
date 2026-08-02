# Build context: repo root. Image entrypoint serves both Batch job definitions;
# the job def command supplies argv (correlate | dvv + flags).
FROM python:3.10-slim

WORKDIR /code

# Layer the heavy pinned deps first so code edits don't bust the cache
RUN pip install --no-cache-dir \
    noisepy-seis noisepy-seis-io==0.3.5 "codameter[aws]" \
    pyarrow pandas boto3 s3fs

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

ENTRYPOINT ["python", "-m", "noisepy_dvv_cloud"]
