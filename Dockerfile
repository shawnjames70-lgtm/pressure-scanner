FROM python:3.11-slim

WORKDIR /app

# Install system dependencies including tzdata for zoneinfo
RUN apt-get update && apt-get install -y \
    build-essential \
    curl \
    tzdata \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy app files
COPY . .

# Create streamlit config directory
RUN mkdir -p /root/.streamlit

# Streamlit config for HF Spaces — dark theme, no CORS issues
RUN printf '[server]\nport = 7860\naddress = "0.0.0.0"\nheadless = true\nenableCORS = false\nenableXsrfProtection = false\n\n[browser]\ngatherUsageStats = false\n\n[theme]\nbase = "dark"\nbackgroundColor = "#040609"\nsecondaryBackgroundColor = "#060a12"\ntextColor = "#c9d1d9"\nprimaryColor = "#0066ff"\n' > /root/.streamlit/config.toml

EXPOSE 7860

CMD ["python3", "-m", "streamlit", "run", "app.py", "--server.port=7860", "--server.address=0.0.0.0"]
