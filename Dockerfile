FROM python:3.11-slim

WORKDIR /app

COPY . .

RUN pip install --no-cache-dir .

EXPOSE 8000

# Run both API and Worker in the same container
CMD ["bash", "-c", "python -m market_source_verification_agent.server & python -m market_source_verification_agent.worker; wait"]
