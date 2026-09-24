"""Render an offline, searchable PDF map of UCSC buildings and A-permit parking.

Usage: python main.py PARKING BASEMAP OUTPUT

PARKING is the ArcGIS parking lot GeoJSON (EPSG:3857), BASEMAP is the osmium
GeoJSON export of roads, paths, and buildings (EPSG:4326), and OUTPUT is the
PDF to write. Text is embedded as TrueType so the PDF stays searchable, and
each lot label links to the lot in Google Maps.
"""

import argparse
import datetime
import re
from pathlib import Path

import geopandas as gpd
import matplotlib
import pandas as pd
from adjustText import adjust_text
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.patches import Patch
from matplotlib.text import Text
from matplotlib.transforms import Bbox
from shapely.geometry import Point, Polygon
from shapely.geometry import box as shapely_box

PAGE_SIZE: tuple[float, float] = (8.5, 11.0)
MARGIN: float = 0.4
HEADER: float = 0.6
EXTENT_PADDING: float = 150.0
PREVIEW_NAME: str = "map.png"
PREVIEW_DPI: int = 200

# Lots south of this latitude (Westside Research Park, Coastal Science Campus)
# are about 5 km from the main campus, so they go in a corner inset.
SOUTH_LATITUDE: float = 36.965
# The main map's bottom edge sits this many meters below this lot.
SOUTH_BORDER_LOT: str = "127"
SOUTH_BORDER_MARGIN: float = 60.0
# Inset position and size in inches from the lower-left corner of the page;
# upper left, in the empty band left above campus once the south edge is fixed.
INSET_RECT: tuple[float, float, float, float] = (0.55, 7.95, 3.0, 1.85)
INSET_TITLE: str = "Westside & Coastal Science Campus (5 km south)"
# The inset is drawn at a smaller scale, so labels need more room in meters.
INSET_PADDING: float = 600.0

# adjustText padding around each label (x, y); y is larger to make room for
# the enforcement line under each label.
LABEL_EXPAND: tuple[float, float] = (1.05, 2.2)

PARKING_CRS: str = "EPSG:3857"
PLOT_CRS: str = "EPSG:32610"
LINK_CRS: str = "EPSG:4326"

ROAD_TYPES: set[str] = {
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "service",
    "living_street",
}
PATH_TYPES: set[str] = {
    "footway",
    "path",
    "pedestrian",
    "steps",
    "cycleway",
    "bridleway",
    "track",
}

ROAD_COLOR: str = "#b0b0b0"
PATH_COLOR: str = "#c8c8c8"
BUILDING_FILL: str = "#ececec"
BUILDING_EDGE: str = "#d0d0d0"
BUILDING_TEXT: str = "#606060"
LOT_EDGE: str = "#5e35b1"
LOT_TEXT: str = "#222222"

# Lot fill by PERMIT_SPACES: (minimum spaces, fill color, legend label),
# largest first.
SIZE_TIERS: list[tuple[int, str, str]] = [
    (100, "#a887dd", "100+ spaces"),
    (25, "#cbb6ee", "25–99 spaces"),
    (0, "#e8def8", "1–24 spaces"),
]

DAY_NAMES: dict[str, str] = {"Mon-Fri": "M-F", "Daily": "Daily"}
TIME_PATTERN: re.Pattern[str] = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])m", re.I)


def shorten_time(match: re.Match[str]) -> str:
    """Shorten one matched clock time, dropping ':00' minutes."""
    hour: str = match.group(1)
    minute: str = match.group(2)
    suffix: str = match.group(3).lower()
    return f"{hour}{suffix}" if minute == "00" else f"{hour}:{minute}{suffix}"


def shorten_enforcement(text: str | None) -> str:
    """Shorten an ENFORCEMENT value for the small info line under a lot label.

    >>> shorten_enforcement("Mon-Fri, 7:00am-5:00pm")
    'M-F 7a-5p'
    >>> shorten_enforcement("Daily, 7:45am-8:30pm")
    'Daily 7:45a-8:30p'
    >>> shorten_enforcement("Daily, 24 hours")
    '24h'
    >>> shorten_enforcement("Mon-Fri, 24 hours")
    'M-F 24h'
    >>> shorten_enforcement(None)
    ''
    """
    if not text:
        return ""
    days: str
    hours: str
    days, _, hours = (part.strip() for part in text.partition(","))
    hours = TIME_PATTERN.sub(shorten_time, hours)
    hours = re.sub(r"24\s*hours?", "24h", hours, flags=re.I)
    if hours == "24h" and days == "Daily":
        return "24h"
    return " ".join(part for part in (DAY_NAMES.get(days, days), hours) if part)


def lot_color(spaces: int) -> str:
    """Return the fill color for a lot with the given number of permit spaces."""
    minimum: int
    color: str
    for minimum, color, _ in SIZE_TIERS:
        if spaces >= minimum:
            return color
    return SIZE_TIERS[-1][1]


def maps_url(point: Point) -> str:
    """Return a Google Maps URL for a point in EPSG:4326."""
    return (
        f"https://www.google.com/maps/search/?api=1&query={point.y:.6f},{point.x:.6f}"
    )


def load_parking(path: Path) -> gpd.GeoDataFrame:
    """Read parking lots and project them to the plotting CRS."""
    lots: gpd.GeoDataFrame = gpd.read_file(path)
    if lots.crs is None:
        lots = lots.set_crs(PARKING_CRS)
    return lots.to_crs(PLOT_CRS)


def split_lots(
    lots: gpd.GeoDataFrame,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Split lots into the main campus and the southern lots for the inset."""
    latitudes: pd.Series = lots.representative_point().to_crs(LINK_CRS).y
    south: pd.Series = latitudes < SOUTH_LATITUDE
    return lots[~south], lots[south]


def south_border(lots: gpd.GeoDataFrame) -> float | None:
    """Return the main map's bottom edge, just below SOUTH_BORDER_LOT."""
    border: gpd.GeoDataFrame = lots[lots["LOT"] == SOUTH_BORDER_LOT]
    if border.empty:
        return None
    return float(border.total_bounds[1]) - SOUTH_BORDER_MARGIN


def load_basemap(
    path: Path,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Read the OSM basemap and split it into roads, paths, and buildings."""
    features: gpd.GeoDataFrame = gpd.read_file(path)
    if features.crs is None:
        features = features.set_crs(LINK_CRS)
    features = features.to_crs(PLOT_CRS)
    column: str
    for column in ("highway", "building", "name"):
        if column not in features:
            features[column] = None
    lines: gpd.GeoDataFrame = features[
        features.geom_type.isin(["LineString", "MultiLineString"])
    ]
    roads: gpd.GeoDataFrame = lines[lines["highway"].isin(ROAD_TYPES)]
    paths: gpd.GeoDataFrame = lines[lines["highway"].isin(PATH_TYPES)]
    buildings: gpd.GeoDataFrame = features[
        features["building"].notna()
        & features.geom_type.isin(["Polygon", "MultiPolygon"])
    ]
    return roads, paths, buildings


def set_extent(
    ax: Axes,
    lots: gpd.GeoDataFrame,
    padding: float = EXTENT_PADDING,
    south: float | None = None,
) -> Polygon:
    """Fit the map to the parking lots with a margin, keeping true shape.

    If south is given, it fixes the bottom edge and any extra height needed to
    fill the axes is added to the north. Returns the visible extent.
    """
    minx: float
    miny: float
    maxx: float
    maxy: float
    minx, miny, maxx, maxy = lots.total_bounds
    left: float = minx - padding
    right: float = maxx + padding
    bottom: float = miny - padding if south is None else south
    top: float = maxy + padding
    width: float = right - left
    height: float = top - bottom
    box: Bbox = ax.get_position(original=True)
    figure_width: float
    figure_height: float
    figure_width, figure_height = ax.figure.get_size_inches()
    box_ratio: float = (box.height * figure_height) / (box.width * figure_width)
    # Grow the shorter side so the map fills the axes at a 1:1 scale.
    extra: float
    if height / width < box_ratio:
        extra = width * box_ratio - height
        if south is None:
            bottom -= extra / 2
            top += extra / 2
        else:
            top += extra
    else:
        extra = height / box_ratio - width
        left -= extra / 2
        right += extra / 2
    ax.set_xlim(left, right)
    ax.set_ylim(bottom, top)
    ax.set_aspect("equal")
    ax.set_axis_off()
    return shapely_box(left, bottom, right, top)


def draw_basemap(
    ax: Axes,
    roads: gpd.GeoDataFrame,
    paths: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
) -> None:
    """Draw buildings, paths, and roads in muted grays."""
    if not buildings.empty:
        buildings.plot(
            ax=ax,
            facecolor=BUILDING_FILL,
            edgecolor=BUILDING_EDGE,
            linewidth=0.2,
            zorder=1,
        )
    if not paths.empty:
        paths.plot(
            ax=ax, color=PATH_COLOR, linewidth=0.3, linestyle=(0, (2, 1)), zorder=2
        )
    if not roads.empty:
        roads.plot(ax=ax, color=ROAD_COLOR, linewidth=0.8, zorder=2)


def draw_building_labels(ax: Axes, buildings: gpd.GeoDataFrame) -> None:
    """Label named buildings at a point inside each polygon."""
    named: gpd.GeoDataFrame = buildings[buildings["name"].notna()]
    name: str
    point: Point
    for name, point in zip(named["name"], named.representative_point()):
        ax.text(
            point.x,
            point.y,
            name,
            fontsize=3.5,
            fontstyle="italic",
            color=BUILDING_TEXT,
            ha="center",
            va="center",
            zorder=3,
            clip_on=True,
        )


def draw_lots(ax: Axes, lots: gpd.GeoDataFrame) -> None:
    """Fill lot polygons by size."""
    lots.plot(
        ax=ax,
        color=[lot_color(int(spaces)) for spaces in lots["PERMIT_SPACES"]],
        edgecolor=LOT_EDGE,
        linewidth=0.5,
        zorder=4,
    )


def draw_lot_labels(ax: Axes, lots: gpd.GeoDataFrame) -> None:
    """Label each lot with a link to Google Maps, spreading out overlaps.

    adjustText moves the bold label lines apart and draws a leader line back
    to the lot; the smaller enforcement line is attached below its label so
    it moves with it. Call after the extent is set, since adjustText works in
    display space.
    """
    anchors: gpd.GeoSeries = lots.representative_point()
    links: gpd.GeoSeries = anchors.to_crs(LINK_CRS)
    labels: list[Text] = []
    lot: dict[str, object]
    anchor: Point
    link: Point
    for lot, anchor, link in zip(lots.to_dict("records"), anchors, links):
        url: str = maps_url(link)
        label: Text = ax.text(
            anchor.x,
            anchor.y,
            f"{lot['LOT']}: {lot['NAME']} ({lot['PERMIT_SPACES']})",
            fontsize=5,
            fontweight="bold",
            color=LOT_TEXT,
            ha="center",
            va="center",
            url=url,
            zorder=6,
            clip_on=True,
        )
        ax.annotate(
            shorten_enforcement(lot.get("ENFORCEMENT")),
            xy=(0.5, 0),
            xycoords=label,
            xytext=(0, -0.5),
            textcoords="offset points",
            fontsize=4,
            color=LOT_TEXT,
            ha="center",
            va="top",
            url=url,
            zorder=6,
            clip_on=True,
        )
        labels.append(label)
    adjust_text(
        labels,
        ax=ax,
        target_x=list(anchors.x),
        target_y=list(anchors.y),
        # Extra vertical room leaves space for the enforcement line below.
        expand=LABEL_EXPAND,
        ensure_inside_axes=True,
        arrowprops={"arrowstyle": "-", "color": LOT_EDGE, "linewidth": 0.3},
    )


def draw_map(
    ax: Axes,
    lots: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    paths: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    padding: float = EXTENT_PADDING,
    south: float | None = None,
) -> None:
    """Draw the basemap and lots, zoom to the lots, then place lot labels."""
    draw_basemap(ax, roads, paths, buildings)
    draw_building_labels(ax, buildings)
    draw_lots(ax, lots)
    extent: Polygon = set_extent(ax, lots, padding, south)
    # Only label lots on the map; adjustText would pull others onto the page.
    draw_lot_labels(ax, lots[lots.representative_point().within(extent)])


def draw_inset(
    figure: Figure,
    lots: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    paths: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
) -> None:
    """Draw the southern lots in a framed corner map at INSET_RECT."""
    if lots.empty:
        return
    left: float
    bottom: float
    width: float
    height: float
    left, bottom, width, height = INSET_RECT
    inset: Axes = figure.add_axes(
        (
            left / PAGE_SIZE[0],
            bottom / PAGE_SIZE[1],
            width / PAGE_SIZE[0],
            height / PAGE_SIZE[1],
        ),
        zorder=10,
    )
    inset.set_facecolor("white")
    draw_map(inset, lots, roads, paths, buildings, INSET_PADDING)
    # set_extent hides the axes; bring back the frame without ticks.
    inset.set_axis_on()
    inset.set_xticks([])
    inset.set_yticks([])
    inset.set_title(INSET_TITLE, fontsize=6, loc="left", pad=3)


def draw_header(figure: Figure, ax: Axes) -> None:
    """Add the title, update date, and lot size legend."""
    top: float = 1 - MARGIN / PAGE_SIZE[1]
    left: float = MARGIN / PAGE_SIZE[0]
    figure.text(
        left, top, "UCSC parking for A permits", fontsize=14, weight="bold", va="top"
    )
    figure.text(
        left,
        top - 0.022,
        f"Updated {datetime.date.today().isoformat()}"
        " · tap a lot label to open it in Google Maps",
        fontsize=7,
        color="#555555",
        va="top",
    )
    handles: list[Patch] = [
        Patch(facecolor=color, edgecolor=LOT_EDGE, linewidth=0.5, label=label)
        for _, color, label in SIZE_TIERS
    ]
    ax.legend(
        handles=handles,
        loc="upper right",
        fontsize=6,
        title="Permit spaces",
        title_fontsize=6,
        framealpha=0.9,
    )


def render(parking_path: Path, basemap_path: Path, output_path: Path) -> None:
    """Draw the map and write it to a letter-size PDF."""
    lots: gpd.GeoDataFrame = load_parking(parking_path)
    roads: gpd.GeoDataFrame
    paths: gpd.GeoDataFrame
    buildings: gpd.GeoDataFrame
    roads, paths, buildings = load_basemap(basemap_path)

    figure: Figure = Figure(figsize=PAGE_SIZE)
    # adjustText measures text through a renderer; savefig still writes PDF.
    FigureCanvasAgg(figure)
    width: float = 1 - 2 * MARGIN / PAGE_SIZE[0]
    height: float = 1 - (2 * MARGIN + HEADER) / PAGE_SIZE[1]
    ax: Axes = figure.add_axes(
        (MARGIN / PAGE_SIZE[0], MARGIN / PAGE_SIZE[1], width, height)
    )

    campus: gpd.GeoDataFrame
    south: gpd.GeoDataFrame
    campus, south = split_lots(lots)
    draw_map(ax, campus, roads, paths, buildings, south=south_border(campus))
    draw_inset(figure, south, roads, paths, buildings)
    draw_header(figure, ax)

    figure.savefig(
        output_path,
        metadata={"Title": "UCSC parking for A permits"},
    )
    # Raster copy next to the PDF for a quick preview.
    figure.savefig(output_path.with_name(PREVIEW_NAME), dpi=PREVIEW_DPI)


def main() -> None:
    """Parse command line arguments and render the map."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0]
    )
    parser.add_argument("parking", type=Path, help="parking lot GeoJSON")
    parser.add_argument("basemap", type=Path, help="OSM basemap GeoJSON")
    parser.add_argument("output", type=Path, help="PDF to write")
    args: argparse.Namespace = parser.parse_args()
    render(args.parking, args.basemap, args.output)


# Embed TrueType fonts so PDF text stays selectable and searchable.
matplotlib.rcParams["pdf.fonttype"] = 42

if __name__ == "__main__":
    main()
