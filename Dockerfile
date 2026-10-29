# Use an official Python runtime as a parent image
FROM python:3.11-slim

# Set the working directory in the container
WORKDIR /app

# Copy the current directory contents into the container at /app
COPY . /app

# Install the required packages
RUN pip install --no-cache-dir fastapi uvicorn httpx python-dotenv jinja2 astor black

# Expose ports for both servers
EXPOSE 8000
EXPOSE 8001

# This ensures BOTH servers stay in the foreground so you can see their logs
CMD ["sh", "-c", "uvicorn mcp_server:app --host 0.0.0.0 --port 8000 & uvicorn web_server:app --host 0.0.0.0 --port 8001"]

# Add 'astor' to the list of installed packages
RUN pip install --no-cache-dir fastapi uvicorn httpx python-dotenv jinja2 astor