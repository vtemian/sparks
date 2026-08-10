# An image for the examples. Build and submit in one step:
#
#   sparks submit --context ./examples --data ./examples/data \
#     --name smoke -- python /app/smoke_loop.py
#
# The container's working directory is the shared dir, not this one, so the
# command names the script by absolute path.
FROM python:3.12-slim

WORKDIR /app

# Just the metrics half of sparks; the client and the queue server stay on the
# laptop and the box. A tarball rather than git+https, because slim images carry
# no git. Pin a tag instead of main in anything you care about.
RUN pip install --no-cache-dir \
    "https://github.com/vtemian/sparks/archive/refs/heads/main.tar.gz"

COPY *.py /app/
