FROM public.ecr.aws/lambda/python:3.14

# boto3 is already in the Lambda base image.
RUN pip install --no-cache-dir --target "${LAMBDA_TASK_ROOT}" \
    adjusttext \
    geopandas \
    matplotlib \
    pdfplumber \
    shapely

COPY main.py buildings.py lambda_handler.py "${LAMBDA_TASK_ROOT}/"
# Built locally (make basemap labels); they only change when OSM or the
# poster does, so they ship with the image instead of being rebuilt daily.
COPY data/basemap.geojson data/poster_labels.csv "${LAMBDA_TASK_ROOT}/data/"

# Passed in by `make build` so the Makefile stays the one place it's defined.
ARG PARKING_URL
ENV PARKING_URL=${PARKING_URL}
ENV MPLCONFIGDIR=/tmp

CMD ["lambda_handler.lambda_handler"]
