# Use a lightweight Python base image to keep the container efficient
FROM python:3.11-slim

WORKDIR /

COPY requirements.txt .

RUN pip install  -r requirements.txt

COPY . .

CMD ["python", "engine.py"]