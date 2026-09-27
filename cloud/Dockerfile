FROM python:3.12-slim
WORKDIR /app
RUN pip install --no-cache-dir cryptography==45.0.5
COPY server.py /app/server.py
COPY feedback_store.py feedback_contract.py matrix_store.py admin.py /app/
COPY site /app/site
USER 10001:10001
EXPOSE 8789
CMD ["python", "-u", "/app/server.py"]
