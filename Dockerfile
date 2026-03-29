# Dockerfile
FROM python:3.12-slim
# Allow statements and log messages to immediately appear in the logs
ENV PYTHONUNBUFFERED True
# Copy local code to the container image.
ENV APP_HOME /back-end
WORKDIR $APP_HOME
COPY . ./

#RUN apt-get update && apt-get install -y \
    #libzbar0 \
    #ffmpeg \
    #&& rm -rf /var/lib/apt/lists/*
    
RUN pip install --no-cache-dir --upgrade pip

# Cache-busting comment to force re-install: 2024-05-22
RUN pip install --no-cache-dir -r requirements.txt

# Run the web service on container startup. Here we use the gunicorn
# webserver, with one worker process and 8 threads.
# For environments with multiple CPU cores, increase the number of workers
# to be equal to the cores available.
# Timeout is set to 0 to disable the timeouts of the workers to allow Cloud Run to handle instance scaling.
CMD exec gunicorn --bind :$PORT --workers 1 --worker-class uvicorn.workers.UvicornWorker --timeout 0 app:app
