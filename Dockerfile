FROM python:3.13-slim
WORKDIR /app
COPY superaimail/ /app/superaimail/
RUN groupadd --gid 10001 superaimail && useradd --uid 10001 --gid superaimail --no-create-home superaimail \
    && mkdir /data && chown superaimail:superaimail /data
USER 10001:10001
EXPOSE 8787
ENV PYTHONUNBUFFERED=1 SUPERMAIL_DATA=/data SUPERMAIL_HOST=0.0.0.0
CMD ["python", "-m", "superaimail", "--no-browser"]
