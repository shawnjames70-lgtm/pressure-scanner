FROM python:3.11-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Create streamlit config directory
RUN mkdir -p /root/.streamlit

# Streamlit config for HF Spaces
RUN echo '[server]\nport = 7860\naddress = "0.0.0.0"\nheadless = true\nenableCORS = false\nenableXsrfProtection = false\n\n[browser]\ngatherUsageStats = false\n\n[theme]\nbase = "dark"\nprimaryColor = "#00ff88"\nbackgroundColor = "#0a0a0a"\nsecondaryBackgroundColor = "#1a1a1a"\ntextColor = "#ffffff"' > /root/.streamlit/config.toml

EXPOSE 7860

CMD ["python3", "-m", "streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]
