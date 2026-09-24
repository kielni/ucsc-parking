# Build an offline, searchable PDF map of UCSC buildings and A-permit parking.
#
#   make all       fetch data (basemap only if missing) and render the PDF
#   make parking   refetch parking lots (daily)
#   make basemap   build the OSM basemap if missing
#   make refresh-basemap   rebuild the basemap from a fresh Geofabrik download


# Campus bounding box (lon/lat): parking lot extent plus a small margin.
BBOX := -122.075,36.945,-122.045,37.005

# A-permit lots with permit spaces; see TODO.md for the decoded where clause.
PARKING_URL := https://services3.arcgis.com/21H3muniXm83m5hZ/arcgis/rest/services/Join_Features_to_Parking_Lots_2_view/FeatureServer/0/query?f=geojson&cacheHint=true&maxRecordCountFactor=4&resultOffset=0&resultRecordCount=8000&where=%27%2C%20%27%20%7C%7C%20PERMITS_ACCEPTED%20%7C%7C%20%27%2C%27%20LIKE%20%27%25%2C%20A%5B%2C%20%5D%25%27%20AND%20PERMIT_SPACES%20%3E%200&orderByFields=ObjectId%20ASC&outFields=NAME%2CLOT%2CF10_MIN%2CF15_MIN%2CF20_MIN%2CF3_HOUR%2CPERMIT_SPACES%2CENFORCEMENT%2CPERMITS_ACCEPTED%2CObjectId&outSR=102100&spatialRel=esriSpatialRelIntersects
PARKING_FIELDS := ["NAME","LOT","PERMIT_SPACES","ENFORCEMENT","PERMITS_ACCEPTED"]

NORCAL_URL := https://download.geofabrik.de/north-america/us/california/norcal-latest.osm.pbf
NORCAL_PBF := data/norcal-latest.osm.pbf
CAMPUS_PBF := data/campus.osm.pbf
FILTERED_PBF := data/campus-filtered.osm.pbf

PARKING := data/parking.geojson
BASEMAP := data/basemap.geojson
PDF := campus-parking.pdf

.PHONY: all fetch parking basemap refresh-basemap render lint clean distclean

all: fetch render

fetch: parking basemap

# Always refetch; keep the previous file if the download or validation fails.
parking: | data
	curl --fail --silent --show-error --output $(PARKING).tmp '$(PARKING_URL)'
	jq -e '(.features | length) > 0 and all(.features[].properties; keys as $$k | $(PARKING_FIELDS) - $$k == [])' \
		$(PARKING).tmp > /dev/null \
		|| { echo "parking: no features or missing fields" >&2; rm -f $(PARKING).tmp; exit 1; }
	mv $(PARKING).tmp $(PARKING)
	@echo "parking: $$(jq '.features | length' $(PARKING)) lots"

$(PARKING):
	$(MAKE) parking

basemap: $(BASEMAP)

# The NorCal extract is large, so it and the clipped pbfs are removed after use.
.INTERMEDIATE: $(NORCAL_PBF) $(CAMPUS_PBF) $(FILTERED_PBF)

# downloads ~650M file
$(NORCAL_PBF): | data
	curl --fail --location --show-error --output $@ '$(NORCAL_URL)'

$(CAMPUS_PBF): $(NORCAL_PBF)
	osmium extract --bbox $(BBOX) --overwrite --output $@ $<

$(FILTERED_PBF): $(CAMPUS_PBF)
	osmium tags-filter --overwrite --output $@ $< nw/highway wr/building

$(BASEMAP): $(FILTERED_PBF)
	osmium export --overwrite --output-format geojson --output $@ $<

refresh-basemap:
	rm -f $(BASEMAP)
	$(MAKE) basemap

render: $(PDF)

$(PDF): main.py $(PARKING) $(BASEMAP)
	@# also writes map.png next to the PDF for previewing
	uv run main.py $(PARKING) $(BASEMAP) $@

lint:
	black main.py

data:
	mkdir -p $@

clean:
	rm -f $(PDF) map.png data/*.tmp

distclean: clean
	rm -rf data
