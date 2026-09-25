FROM public.ecr.aws/lambda/python:3.14

# Install the exact versions in uv.lock (the ones pyproject.toml resolved to
# locally), with their hashes checked. uv, pyproject.toml, and uv.lock are
# only mounted for this step, so none of them end up in the image.
RUN --mount=from=ghcr.io/astral-sh/uv:0.11.11,source=/uv,target=/bin/uv \
    --mount=type=bind,source=pyproject.toml,target=/tmp/project/pyproject.toml \
    --mount=type=bind,source=uv.lock,target=/tmp/project/uv.lock \
    uv export --frozen --no-dev --no-emit-project --project /tmp/project \
        --output-file /tmp/requirements.txt \
    && uv pip install --no-cache --target "${LAMBDA_TASK_ROOT}" \
        --requirements /tmp/requirements.txt \
    && rm /tmp/requirements.txt

COPY main.py lambda_handler.py "${LAMBDA_TASK_ROOT}/"
# Built locally (make basemap labels); they only change when OSM or the
# poster does, so they ship with the image instead of being rebuilt daily.
# The labels CSV already has each label's map position, so buildings.py
# isn't needed here.
COPY data/basemap.geojson data/poster_labels.csv "${LAMBDA_TASK_ROOT}/data/"

# Passed in by `make build` so the Makefile stays the one place it's defined.
ARG PARKING_URL
ENV PARKING_URL=${PARKING_URL}
ENV MPLCONFIGDIR=/tmp

CMD ["lambda_handler.lambda_handler"]
