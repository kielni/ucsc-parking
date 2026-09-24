"""Render an offline, searchable PDF map of UC Santa Cruz buildings and parking.

Usage: python main.py PARKING BASEMAP LABELS OUTPUT

PARKING is the ArcGIS parking lot GeoJSON (EPSG:3857), BASEMAP is the osmium
GeoJSON export of roads, paths, and buildings (EPSG:4326), LABELS is the
building label CSV from buildings.py, and OUTPUT is the PDF to write. Text is
embedded as TrueType so the PDF stays searchable, and each lot label links to
the lot in Google Maps.
"""

import argparse
import datetime
import re
from pathlib import Path

import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
from adjustText import adjust_text
from matplotlib.axes import Axes
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from matplotlib.legend import Legend
from matplotlib.patches import Patch, Rectangle
from matplotlib.patheffects import withStroke
from matplotlib.text import Text
from matplotlib.transforms import Bbox
from shapely.geometry import Point, Polygon
from shapely.geometry import box as shapely_box

from buildings import normalize

PAGE_SIZE: tuple[float, float] = (8.5, 11.0)
MARGIN: float = 0.25
HEADER: float = 0.6
# Meters of map beyond the outermost lots; labels stay inside the axes anyway.
EXTENT_PADDING: float = 40.0
PREVIEW_DPI: int = 200

TITLE: str = "UC Santa Cruz student parking"

# Lots south of this latitude (Westside Research Park, Coastal Science Campus)
# are about 5 km from the main campus and are left off the map.
SOUTH_LATITUDE: float = 36.965
# The main map's bottom edge sits this many meters below this lot.
SOUTH_BORDER_LOT: str = "127"
SOUTH_BORDER_MARGIN: float = 60.0

# adjustText padding around each label (x, y). Lot labels only move
# vertically, and their y padding leaves room for the enforcement line.
LABEL_EXPAND: tuple[float, float] = (1.1, 1.4)
LOT_LABEL_EXPAND: tuple[float, float] = (1.05, 2.2)
# Lot and building names share a size; building names are bold. Their
# colors differ in hue (purple, brown) but match in darkness.
LABEL_FONT_SIZE: float = 5.0
LABEL_WEIGHT: str = "normal"
BUILDING_WEIGHT: str = "bold"
BUILDING_FORCE_STATIC: tuple[float, float] = (0.5, 1.0)
# Points per side of the grid that stands in for each fixed label.
OBSTACLE_GRID: int = 4

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
PATH_COLOR: str = "#6a9f58"
BUILDING_FILL: str = "#e6d5b8"
BUILDING_EDGE: str = "#a88b5f"
BUILDING_TEXT: str = "#4a3b26"
LOT_EDGE: str = "#5e35b1"
# Near-black purple so lot names stand out on the purple lot fills.
LOT_TEXT: str = "#1f1238"
# Soft white outline behind lot labels: width in points, and opacity.
HALO_COLOR: str = "white"
HALO_WIDTH: float = 1.5
HALO_ALPHA: float = 0.75
# Dropped from building names on the map, with any "/" joining it.
BUILDING_WORD: re.Pattern[str] = re.compile(r"\s*/?\b(Building|Bldg)\b", re.I)
# Building names longer than this (characters) wrap onto two lines.
WRAP_LENGTH: int = 20

# Lot fill by PERMIT_SPACES: (minimum spaces, fill color, legend label),
# largest first.
# Scale bar length, and walking speed (m/s) for its "min walk" note.
SCALE_FEET: int = 1000
METERS_PER_FOOT: float = 0.3048
WALK_SPEED: float = 1.4
SCALE_COLOR: str = "#333333"
# Alternating black/white segments; gap from the map edge in bar heights.
SCALE_SEGMENTS: int = 4
SCALE_GAP: float = 1.0
SCALE_FONT_SIZE: float = 6.0

# Data credits under the map, and their gap below it (inches).
CREDITS: str = (
    "Basemap: © OpenStreetMap contributors · Parking: UCSC TAPS ·"
    " Buildings: The Center for Integrated Spatial Research, UC Santa Cruz"
)
CREDITS_GAP: float = 0.05

# Legend line explaining the number in parentheses on lot labels.
LEGEND_KEY: str = "Lot ___ (spaces)"
SIZE_TIERS: list[tuple[int, str, str]] = [
    (100, "#a887dd", "100+ spaces"),
    (25, "#cbb6ee", "25-99 spaces"),
    (0, "#e8def8", "1-24 spaces"),
]

# Poster labels are placed with an affine fit from poster points to map
# meters, using labels whose names match an OSM building as control points.
# Control points farther than OUTLIER_DISTANCE (m) from the fit are dropped,
# and a placed label within SNAP_DISTANCE (m) of a building moves onto it.
MIN_CONTROL_POINTS: int = 6
OUTLIER_DISTANCE: float = 40.0
SNAP_DISTANCE: float = 25.0
# A label goes on the OSM building with its name if that building (all parts
# together) fits within this diagonal (m).
NAME_MATCH_EXTENT: float = 150.0

# Enforced days as shown on the map; "Daily" is the default, so it's omitted.
DAY_NAMES: dict[str, str] = {"Mon-Fri": "M-F", "Daily": ""}
TIME_PATTERN: re.Pattern[str] = re.compile(r"(\d{1,2}):(\d{2})\s*([ap])m", re.I)


def shorten_time(match: re.Match[str]) -> str:
    """Shorten one matched clock time, dropping ':00' minutes."""
    hour: str = match.group(1)
    minute: str = match.group(2)
    suffix: str = match.group(3).lower()
    return f"{hour}{suffix}" if minute == "00" else f"{hour}:{minute}{suffix}"


def shorten_enforcement(text: str | None) -> str:
    """Shorten an ENFORCEMENT value for the small info line under a lot label.

    "Mon-Fri, 7:00am-5:00pm" -> 'M-F 7a-5p'
    "Daily, 7:45am-8:30pm" -> '7:45a-8:30p'
    "Daily, 24 hours" -> '24h'
    "Mon-Fri, 24 hours" -> 'M-F 24h'
    """
    if not text:
        return ""
    days: str
    hours: str
    days, _, hours = (part.strip() for part in text.partition(","))
    hours = TIME_PATTERN.sub(shorten_time, hours)
    hours = re.sub(r"24\s*hours?", "24h", hours, flags=re.I)
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


def campus_lots(lots: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Drop the southern lots, keeping those on the main campus."""
    latitudes: pd.Series = lots.representative_point().to_crs(LINK_CRS).y
    return lots[latitudes >= SOUTH_LATITUDE]


def south_border(lots: gpd.GeoDataFrame) -> float:
    """Return the main map's bottom edge, just below SOUTH_BORDER_LOT.

    Falls back to the southernmost lot if SOUTH_BORDER_LOT is missing.
    """
    border: gpd.GeoDataFrame = lots[lots["LOT"] == SOUTH_BORDER_LOT]
    if border.empty:
        border = lots
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
    padding: float,
    south: float,
) -> Polygon:
    """Fit the map to the parking lots with a margin, keeping true shape.

    south fixes the bottom edge. If the lots are wider than the axes'
    shape, the axes get shorter (keeping their top edge) rather than showing
    empty map to the north; if narrower, the map widens evenly. Returns the
    visible extent.
    """
    minx, miny, maxx, maxy = lots.total_bounds
    left: float = minx - padding
    right: float = maxx + padding
    bottom: float = south
    top: float = maxy + padding
    width: float = right - left
    height: float = top - bottom
    box: Bbox = ax.get_position(original=True)
    figure_width: float
    figure_height: float
    figure_width, figure_height = ax.figure.get_size_inches()
    box_ratio: float = (box.height * figure_height) / (box.width * figure_width)
    # Keep a 1:1 scale: shrink the axes to a wide map, or widen a tall one.
    if height / width < box_ratio:
        shorter: float = box.height * (height / width) / box_ratio
        ax.set_position((box.x0, box.y1 - shorter, box.width, shorter))
    else:
        extra: float = height / box_ratio - width
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
    """Draw tan buildings, green paths, and gray roads."""
    if not buildings.empty:
        buildings.plot(
            ax=ax,
            facecolor=BUILDING_FILL,
            edgecolor=BUILDING_EDGE,
            linewidth=0.4,
            zorder=1,
        )
    if not paths.empty:
        paths.plot(
            ax=ax, color=PATH_COLOR, linewidth=0.4, linestyle=(0, (2, 1)), zorder=2
        )
    if not roads.empty:
        roads.plot(ax=ax, color=ROAD_COLOR, linewidth=0.8, zorder=2)


def load_poster_labels(path: Path) -> pd.DataFrame:
    """Read building labels extracted from the campus map poster."""
    return pd.read_csv(path, keep_default_na=False)


def display_name(name: str) -> str:
    """Drop the word "Building" (or "Bldg") from a building name for the map.

    "Physical Sciences Building" -> "Physical Sciences"
    "Baytree Bookstore/Building" -> "Baytree Bookstore"
    "Press Bldg" -> "Press"
    """
    return BUILDING_WORD.sub("", name).strip(" /")


def wrap_name(name: str) -> str:
    """Break a long building name before its last word.

    Short last words like "2" stay with the word before them.

    "Science & Engineering Library" -> "Science & Engineering\\nLibrary"
    "Eloise Pickard Smith Gallery" -> "Eloise Pickard Smith\\nGallery"
    "Natural Sciences 2" -> "Natural Sciences 2" (short enough)
    """
    if len(name) <= WRAP_LENGTH:
        return name
    words: list[str] = name.split()
    tail: int = 2 if len(words[-1]) <= 2 and len(words) > 2 else 1
    if len(words) <= tail:
        return name
    return f"{' '.join(words[:-tail])}\n{' '.join(words[-tail:])}"


def named_buildings(buildings: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """Return OSM building outlines by normalized name.

    Buildings split into several polygons with the same name are merged.
    """
    named: gpd.GeoDataFrame = buildings[buildings["name"].notna()].copy()
    named["key"] = named["name"].map(normalize)
    return named.dissolve(by="key").geometry


def fit_poster_transform(
    labels: pd.DataFrame, buildings: gpd.GeoDataFrame
) -> np.ndarray:
    """Fit an affine transform from poster points to map meters.

    Control points are poster labels whose name matches exactly one OSM
    building name; buildings split into several polygons are merged first.
    The worst control point is dropped until all fit within
    OUTLIER_DISTANCE, since a few poster labels sit beside their building.
    Returns a 3x2 matrix: [x, y, 1] @ matrix = [easting, northing].
    """
    anchors: gpd.GeoSeries = named_buildings(buildings).representative_point()
    poster: pd.DataFrame = labels.assign(key=labels["index_name"].map(normalize))
    poster = poster.drop_duplicates("key", keep=False)
    pairs: pd.DataFrame = poster[poster["key"].isin(anchors.index)]
    source: np.ndarray = np.column_stack([pairs["x"], pairs["y"], np.ones(len(pairs))])
    target: np.ndarray = np.column_stack(
        [anchors[pairs["key"]].x, anchors[pairs["key"]].y]
    )
    while len(source) >= MIN_CONTROL_POINTS:
        matrix: np.ndarray = np.linalg.lstsq(source, target, rcond=None)[0]
        errors: np.ndarray = np.linalg.norm(source @ matrix - target, axis=1)
        if errors.max() <= OUTLIER_DISTANCE:
            print(
                f"poster fit: {len(source)} control points,"
                f" RMS {np.sqrt(np.mean(errors**2)):.1f} m"
            )
            return matrix
        worst: int = int(errors.argmax())
        source = np.delete(source, worst, axis=0)
        target = np.delete(target, worst, axis=0)
    raise ValueError("too few poster labels match OSM building names")


def place_poster_labels(
    labels: pd.DataFrame, buildings: gpd.GeoDataFrame
) -> gpd.GeoDataFrame:
    """Place poster labels on the map, on their building where possible.

    A label whose name matches one compact OSM building goes on that
    building. Other labels are placed by the poster fit and snapped to the
    nearest building within SNAP_DISTANCE.
    """
    matrix: np.ndarray = fit_poster_transform(labels, buildings)
    source: np.ndarray = np.column_stack(
        [labels["x"], labels["y"], np.ones(len(labels))]
    )
    placed: np.ndarray = source @ matrix
    points: gpd.GeoDataFrame = gpd.GeoDataFrame(
        {"name": labels["index_name"].map(display_name).map(wrap_name)},
        geometry=gpd.points_from_xy(placed[:, 0], placed[:, 1]),
        crs=PLOT_CRS,
    )
    nearest: gpd.GeoDataFrame = gpd.sjoin_nearest(
        points, buildings[["geometry"]], how="left", max_distance=SNAP_DISTANCE
    )
    # Ties return several rows per label; keep the first building.
    nearest = nearest[~nearest.index.duplicated()]
    snapped: pd.Series = nearest["index_right"].dropna()
    centers: gpd.GeoSeries = buildings.representative_point()
    points.loc[snapped.index, "geometry"] = centers.loc[snapped].values

    # Same-named buildings spread across campus (e.g. several Dining Commons)
    # are too ambiguous to place a label by name.
    outlines: gpd.GeoSeries = named_buildings(buildings)
    bounds: pd.DataFrame = outlines.bounds
    diagonal: pd.Series = np.hypot(
        bounds["maxx"] - bounds["minx"], bounds["maxy"] - bounds["miny"]
    )
    compact: gpd.GeoSeries = outlines[diagonal <= NAME_MATCH_EXTENT]
    keys: pd.Series = labels["index_name"].map(normalize)
    unique: pd.Series = ~keys.duplicated(keep=False)
    matched: pd.Series = keys[unique & keys.isin(compact.index)]
    points.loc[matched.index, "geometry"] = (
        compact.representative_point().loc[matched].values
    )
    return points


def draw_building_labels(ax: Axes, labels: gpd.GeoDataFrame, avoid: list[Text]) -> None:
    """Label buildings with their poster names, clear of the lot labels.

    Lot labels stay fixed on their lots, so building labels that collide
    with them (or each other) are nudged instead. Call after the extent is
    set, since collisions are measured in display space.
    """
    texts: list[Text] = []
    name: str
    point: Point
    for name, point in zip(labels["name"], labels.geometry):
        texts.append(
            ax.text(
                point.x,
                point.y,
                name,
                fontsize=LABEL_FONT_SIZE,
                fontweight=BUILDING_WEIGHT,
                color=BUILDING_TEXT,
                ha="center",
                va="center",
                # Above lot fills (4), below lot labels and their halos (5.5-6).
                zorder=5,
                clip_on=True,
            )
        )
    separate_labels(ax, texts, avoid)


def text_boxes(ax: Axes, texts: list[Text]) -> list[Bbox]:
    """Return each text's bounding box in display coordinates."""
    renderer: object = ax.figure.canvas.get_renderer()
    return [text.get_window_extent(renderer) for text in texts]


def obstacle_points(ax: Axes, boxes: list[Bbox]) -> tuple[list[float], list[float]]:
    """Return a grid of points covering each box, in data coordinates.

    adjustText's objects option converts coordinates twice, which puts
    obstacles far off the map, so obstacles are passed as points instead.
    """
    to_data: object = ax.transData.inverted()
    xs: list[float] = []
    ys: list[float] = []
    box: Bbox
    for box in boxes:
        fraction_x: float
        fraction_y: float
        for fraction_x in np.linspace(0, 1, OBSTACLE_GRID):
            for fraction_y in np.linspace(0, 1, OBSTACLE_GRID):
                x: float
                y: float
                x, y = to_data.transform(
                    (
                        box.x0 + fraction_x * box.width,
                        box.y0 + fraction_y * box.height,
                    )
                )
                xs.append(x)
                ys.append(y)
    return xs, ys


def separate_labels(ax: Axes, texts: list[Text], avoid: list[Text]) -> None:
    """Move only the building labels that overlap another label.

    Labels with nothing in the way keep their exact position; the rest are
    spread out by adjustText, treating lot labels and the untouched building
    labels as fixed obstacles.
    """
    boxes: list[Bbox] = text_boxes(ax, texts)
    fixed_boxes: list[Bbox] = text_boxes(ax, avoid)
    crowded: list[int] = [
        index
        for index, box in enumerate(boxes)
        if any(box.overlaps(other) for other in fixed_boxes)
        or any(box.overlaps(other) for j, other in enumerate(boxes) if j != index)
    ]
    if not crowded:
        return
    fixed_boxes += [box for index, box in enumerate(boxes) if index not in crowded]
    xs: list[float]
    ys: list[float]
    xs, ys = obstacle_points(ax, fixed_boxes)
    adjust_text(
        [texts[index] for index in crowded],
        x=xs,
        y=ys,
        ax=ax,
        # A centered label covers its own anchor point, which adjustText
        # otherwise treats as an obstacle and pushes the label off by about
        # half its width.
        avoid_self=False,
        # Vertical moves only: sideways moves drift right and leave the label
        # beside its building rather than over it.
        only_move="y",
        expand=LABEL_EXPAND,
        # Push harder off the fixed labels than the default.
        force_static=BUILDING_FORCE_STATIC,
        ensure_inside_axes=True,
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


def add_halo(ax: Axes, text: Text) -> None:
    """Draw a soft white outline behind text so it reads on the lot fill.

    The outline is a separate copy under the text rather than a path effect
    on it, because path effects draw text as shapes and the PDF would lose
    the searchable label. The copy is pinned to the text's center, so it
    follows when adjustText moves the label.
    """
    ax.annotate(
        text.get_text(),
        xy=(0.5, 0.5),
        xycoords=text,
        ha="center",
        va="center",
        fontsize=text.get_fontsize(),
        fontweight=text.get_fontweight(),
        color=HALO_COLOR,
        alpha=HALO_ALPHA,
        path_effects=[withStroke(linewidth=HALO_WIDTH, foreground=HALO_COLOR)],
        zorder=text.get_zorder() - 0.5,
        clip_on=True,
    )


def draw_lot_labels(ax: Axes, lots: gpd.GeoDataFrame) -> list[Text]:
    """Label each lot on top of its polygon, with a link to Google Maps.

    The bold line sits just above a point inside the lot and the smaller
    enforcement line just below it. Where neighboring lots' labels collide,
    adjustText stacks them by moving them vertically only, with a short
    leader line back to the lot. Returns both lines' text artists so
    building labels can avoid them.
    """
    anchors: gpd.GeoSeries = lots.representative_point()
    links: gpd.GeoSeries = anchors.to_crs(LINK_CRS)
    labels: list[Text] = []
    for lot, anchor, link in zip(lots.to_dict("records"), anchors, links):
        url: str = maps_url(link)
        label: Text = ax.text(
            anchor.x,
            anchor.y,
            f"Lot {lot['LOT']} ({lot['PERMIT_SPACES']})",
            fontsize=LABEL_FONT_SIZE,
            fontweight=LABEL_WEIGHT,
            color=LOT_TEXT,
            ha="center",
            va="bottom",
            url=url,
            zorder=6,
            clip_on=True,
        )
        info: Text = ax.annotate(
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
        add_halo(ax, label)
        add_halo(ax, info)
        labels.extend([label, info])
    # Mostly vertical: sideways moves only separate labels from each other.
    movement: dict[str, str] = {
        "text": "xy",
        "static": "y",
        "explode": "y",
        "pull": "y",
    }
    adjust_text(
        labels[::2],
        ax=ax,
        target_x=list(anchors.x),
        target_y=list(anchors.y),
        expand=LOT_LABEL_EXPAND,
        only_move=movement,
        ensure_inside_axes=True,
        arrowprops={"arrowstyle": "-", "color": LOT_EDGE, "linewidth": 0.3},
    )
    return labels


def draw_map(
    ax: Axes,
    lots: gpd.GeoDataFrame,
    roads: gpd.GeoDataFrame,
    paths: gpd.GeoDataFrame,
    buildings: gpd.GeoDataFrame,
    building_labels: gpd.GeoDataFrame,
    padding: float,
    south: float,
) -> None:
    """Draw the basemap and lots, zoom to the lots, then place all labels."""
    draw_basemap(ax, roads, paths, buildings)
    draw_lots(ax, lots)
    extent: Polygon = set_extent(ax, lots, padding, south)
    # Only label what's on the map; adjustText would pull others onto the page.
    lot_texts: list[Text] = draw_lot_labels(
        ax, lots[lots.representative_point().within(extent)]
    )
    visible: gpd.GeoDataFrame = building_labels[building_labels.within(extent)]
    draw_building_labels(ax, visible, lot_texts)


def draw_header(figure: Figure, ax: Axes) -> None:
    """Add the title, date (top right), tap hint, and lot size legend."""
    top: float = 1 - MARGIN / PAGE_SIZE[1]
    left: float = MARGIN / PAGE_SIZE[0]
    right: float = 1 - left
    today: datetime.date = datetime.date.today()
    figure.text(left, top, TITLE, fontsize=14, weight="bold", va="top")
    # e.g. "Sep 23, 2026"; strftime's %-d (no leading zero) isn't
    # portable. Aligned with the title's baseline so it reads as part of the
    # header rather than a stray note in the corner.
    title_baseline: float = top - 14 / 72 / PAGE_SIZE[1]
    figure.text(
        right,
        title_baseline,
        f"{today:%b} {today.day}, {today.year}",
        fontsize=9,
        color="#333333",
        ha="right",
        va="baseline",
    )
    figure.text(
        left,
        top - 0.022,
        "tap to open in Google Maps",
        fontsize=7,
        color="#555555",
        va="top",
    )
    # First entry is a key to the lot labels: no swatch, just sample text.
    key: Patch = Patch(facecolor="none", edgecolor="none", label=LEGEND_KEY)
    handles: list[Patch] = [key] + [
        Patch(facecolor=color, edgecolor=LOT_EDGE, linewidth=0.5, label=label)
        for _, color, label in SIZE_TIERS
    ]
    legend: Legend = ax.legend(
        handles=handles,
        loc="lower center",
        fontsize=6,
        # Mathtext bold for just the "A"; mathtext uses the same DejaVu Sans.
        title=r"$\mathbf{A}$ permit spaces",
        title_fontsize=6,
        framealpha=0.9,
    )
    legend.get_texts()[0].set_color(LOT_TEXT)


def text_size(ax: Axes, text: str) -> tuple[float, float]:
    """Return the width and height, in data units, of scale bar text."""
    probe: Text = ax.text(0, 0, text, fontsize=SCALE_FONT_SIZE)
    box: Bbox = text_boxes(ax, [probe])[0].transformed(ax.transData.inverted())
    probe.remove()
    return box.width, box.height


def draw_scale_bar(ax: Axes) -> None:
    """Add a scale bar with walking time in the map's bottom-right corner.

    Alternating black and white segments with end caps, distance ticks below,
    and the walking time to the right. The map is in UTM, so data units are
    meters. Call after the extent is set.
    """
    meters: float = SCALE_FEET * METERS_PER_FOOT
    minutes: int = round(meters / WALK_SPEED / 60)
    walk: str = f"about {minutes} min walk"
    height: float = meters / 40
    pad: float = height
    walk_width: float = text_size(ax, walk)[0]
    tick_height: float = text_size(ax, "0")[1]
    _, x1 = ax.get_xlim()
    y0, _ = ax.get_ylim()
    margin: float = SCALE_GAP * height
    left: float = x1 - margin - pad - walk_width - 2 * height - meters
    bottom: float = y0 + margin + pad + tick_height + 1.5 * height
    segment: float = meters / SCALE_SEGMENTS
    style: dict[str, object] = {"zorder": 8, "clip_on": True}
    index: int
    for index in range(SCALE_SEGMENTS):
        ax.add_patch(
            Rectangle(
                (left + index * segment, bottom),
                segment,
                height,
                facecolor=SCALE_COLOR if index % 2 == 0 else "white",
                edgecolor=SCALE_COLOR,
                linewidth=0.5,
                **style,
            )
        )
    end: float
    for end in (left, left + meters):
        ax.plot(
            [end, end],
            [bottom - height, bottom + 2 * height],
            color=SCALE_COLOR,
            linewidth=0.8,
            solid_capstyle="butt",
            **style,
        )
    texts: list[Text] = []
    feet: int
    for feet in (0, SCALE_FEET // 2, SCALE_FEET):
        texts.append(
            ax.text(
                left + feet * METERS_PER_FOOT,
                bottom - 1.5 * height,
                f"{feet:,} ft" if feet == SCALE_FEET else f"{feet:,}",
                fontsize=SCALE_FONT_SIZE,
                color=SCALE_COLOR,
                ha="center",
                va="top",
                **style,
            )
        )
    texts.append(
        ax.text(
            left + meters + 2 * height,
            bottom + height / 2,
            walk,
            fontsize=SCALE_FONT_SIZE,
            color=SCALE_COLOR,
            ha="left",
            va="center",
            **style,
        )
    )
    # Soft white backing, like the legend's, so paths don't cross the bar.
    to_data: object = ax.transData.inverted()
    box: Bbox = Bbox.union(text_boxes(ax, texts)).transformed(to_data)
    backing_left: float = min(box.x0, left) - pad
    backing_bottom: float = box.y0 - pad
    ax.add_patch(
        Rectangle(
            (backing_left, backing_bottom),
            max(box.x1, left + meters) + pad - backing_left,
            bottom + 2 * height + pad - backing_bottom,
            facecolor="white",
            edgecolor="none",
            alpha=0.9,
            zorder=7,
            clip_on=True,
        )
    )


def draw_credits(figure: Figure, ax: Axes) -> None:
    """Add data credits in small text just below the map."""
    box: Bbox = ax.get_position()
    figure.text(
        box.x0,
        box.y0 - CREDITS_GAP / PAGE_SIZE[1],
        CREDITS,
        fontsize=5,
        color="#555555",
        va="top",
    )


def render(
    parking_path: Path, basemap_path: Path, labels_path: Path, output_path: Path
) -> None:
    """Draw the map and write it to a letter-size PDF."""
    lots: gpd.GeoDataFrame = load_parking(parking_path)
    roads: gpd.GeoDataFrame
    paths: gpd.GeoDataFrame
    buildings: gpd.GeoDataFrame
    roads, paths, buildings = load_basemap(basemap_path)
    building_labels: gpd.GeoDataFrame = place_poster_labels(
        load_poster_labels(labels_path), buildings
    )

    figure: Figure = Figure(figsize=PAGE_SIZE)
    # adjustText measures text through a renderer; savefig still writes PDF.
    FigureCanvasAgg(figure)
    width: float = 1 - 2 * MARGIN / PAGE_SIZE[0]
    height: float = 1 - (2 * MARGIN + HEADER) / PAGE_SIZE[1]
    ax: Axes = figure.add_axes(
        (MARGIN / PAGE_SIZE[0], MARGIN / PAGE_SIZE[1], width, height)
    )

    campus: gpd.GeoDataFrame = campus_lots(lots)
    draw_map(
        ax,
        campus,
        roads,
        paths,
        buildings,
        building_labels,
        EXTENT_PADDING,
        south_border(campus),
    )
    draw_header(figure, ax)
    draw_scale_bar(ax)
    draw_credits(figure, ax)

    figure.savefig(output_path, metadata={"Title": TITLE})
    # Raster copy next to the PDF, same name with .png, for a quick preview.
    figure.savefig(output_path.with_suffix(".png"), dpi=PREVIEW_DPI)


def main() -> None:
    """Parse command line arguments and render the map."""
    parser: argparse.ArgumentParser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0]
    )
    parser.add_argument("parking", type=Path, help="parking lot GeoJSON")
    parser.add_argument("basemap", type=Path, help="OSM basemap GeoJSON")
    parser.add_argument("labels", type=Path, help="poster building labels CSV")
    parser.add_argument("output", type=Path, help="PDF to write")
    args: argparse.Namespace = parser.parse_args()
    render(args.parking, args.basemap, args.labels, args.output)


# Embed TrueType fonts so PDF text stays selectable and searchable.
matplotlib.rcParams["pdf.fonttype"] = 42

if __name__ == "__main__":
    main()
