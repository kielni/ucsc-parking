"""Render an offline, searchable PDF map of UC Santa Cruz buildings and parking.

Usage: python main.py PARKING BASEMAP LABELS OUTPUT

PARKING is the ArcGIS parking lot GeoJSON (EPSG:3857), BASEMAP is the osmium
GeoJSON export of roads, paths, and buildings (EPSG:4326), LABELS is the
building label CSV from buildings.py, with each label's position already on
its building, and OUTPUT is the PDF to write. Text is
embedded as TrueType so the PDF stays searchable, and each lot label links to
the lot in Google Maps.
"""

import argparse
from dataclasses import dataclass
from datetime import date
import re
from pathlib import Path
from typing import BinaryIO

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
from matplotlib.transforms import Bbox, Transform
from pypdf import PageObject, PdfReader, PdfWriter
from pypdf.annotations import Link
from pypdf.generic import ArrayObject, NameObject
from shapely.geometry import Polygon
from shapely.geometry import box as shapely_box

# Page size and margin in inches.
PAGE_SIZE = (8.5, 11.0)
MARGIN = 0.25
# Lot shapes are PDF links; their tap area extends this far (points) past the
# lot's bounding box. PDF pages measure 72 points per inch.
LINK_MARGIN = 6.0
POINTS_PER_INCH = 72.0

TITLE = "UC Santa Cruz student parking"

# Lot and building names share a size so they carry equal visual weight.
LABEL_FONT_SIZE = 5.0
# Points per side of the grid that stands in for each fixed label.
OBSTACLE_GRID = 4

PLOT_CRS = "EPSG:32610"
LINK_CRS = "EPSG:4326"


class Color:
    """Map colors, named by what they color."""

    road: str = "#b0b0b0"
    path: str = "#6a9f58"
    building_fill: str = "#e6d5b8"
    building_edge: str = "#a88b5f"
    # Dark brown: a different hue from lot_text but as dark, so building and
    # lot names carry equal weight.
    building_text: str = "#4a3b26"
    # Lot outlines, legend swatch outlines, and leader lines to moved labels.
    lot_edge: str = "#5e35b1"
    # Near-black purple so lot names stand out on the purple lot fills.
    lot_text: str = "#1f1238"
    # Lot fills by size; see SIZE_TIERS.
    lot_fill_large: str = "#a887dd"
    lot_fill_medium: str = "#cbb6ee"
    lot_fill_small: str = "#e8def8"
    # Soft outline behind lot labels (see add_halo).
    halo: str = "white"
    # Scale bar: dark segments, outlines, and end caps; the alternating light
    # segments; distance and walk time text; and the backing behind it all.
    scale_bar: str = "#333333"
    scale_bar_light: str = "white"
    scale_text: str = "#333333"
    scale_backing: str = "white"
    date: str = "#333333"
    tap_hint: str = "#555555"
    credits: str = "#555555"


# Scale bar length.
SCALE_FEET = 1000
METERS_PER_FOOT = 0.3048
# Alternating dark/light segments.
SCALE_SEGMENTS = 4
SCALE_FONT_SIZE = 6.0

# Lot fill by PERMIT_SPACES: (minimum spaces, fill color, legend label),
# largest first.
SIZE_TIERS: list[tuple[int, str, str]] = [
    (100, Color.lot_fill_large, "100+ spaces"),
    (25, Color.lot_fill_medium, "25-99 spaces"),
    (0, Color.lot_fill_small, "1-24 spaces"),
]


def shorten_time(match: re.Match[str]) -> str:
    """Shorten one matched clock time, dropping ':00' minutes."""
    time: dict[str, str] = match.groupdict()
    suffix: str = time["suffix"].lower()
    if time["minute"] == "00":
        return f"{time['hour']}{suffix}"
    return f"{time['hour']}:{time['minute']}{suffix}"


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
    # Clock times such as "7:00am"; see shorten_time.
    hours = re.sub(
        r"(?P<hour>\d{1,2}):(?P<minute>\d{2})\s*(?P<suffix>[ap])m",
        shorten_time,
        hours,
        flags=re.I,
    )
    hours = re.sub(r"24\s*hours?", "24h", hours, flags=re.I)
    # "Daily" is the default, so it's omitted.
    days = {"Mon-Fri": "M-F", "Daily": ""}.get(days, days)
    return " ".join(part for part in (days, hours) if part)


def lot_color(spaces: int) -> str:
    """Return the fill color for a lot with the given number of permit spaces."""
    for minimum, color, _ in SIZE_TIERS:
        if spaces >= minimum:
            return color
    return SIZE_TIERS[-1][1]


def maps_url(longitude: float, latitude: float) -> str:
    """Return a Google Maps URL for a location in EPSG:4326 degrees."""
    return f"https://www.google.com/maps/search/?api=1&query={latitude:.6f},{longitude:.6f}"


def load_parking(path: Path) -> gpd.GeoDataFrame:
    """Read parking lots and project them to the plotting CRS."""
    lots: gpd.GeoDataFrame = gpd.read_file(path)
    if lots.crs is None:
        # The ArcGIS query returns Web Mercator (outSR=102100).
        lots = lots.set_crs("EPSG:3857")
    return lots.to_crs(PLOT_CRS)


def campus_lots(lots: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """Drop the southern lots, keeping those on the main campus."""
    latitudes: pd.Series = lots.representative_point().to_crs(LINK_CRS).y
    # Lots south of this latitude (Westside Research Park, Coastal Science
    # Campus) are about 5 km from the main campus and are left off the map.
    return lots[latitudes >= 36.965]


def south_border(lots: gpd.GeoDataFrame) -> float:
    """Return the main map's bottom edge, 60 m below Lot 127.

    Falls back to the southernmost lot if Lot 127 is missing.
    """
    border: gpd.GeoDataFrame = lots[lots["LOT"] == "127"]
    if border.empty:
        border = lots
    return float(border.total_bounds[1]) - 60.0


def load_basemap(
    path: Path,
) -> tuple[gpd.GeoDataFrame, gpd.GeoDataFrame, gpd.GeoDataFrame]:
    """Read the OSM basemap and split it into roads, paths, and buildings."""
    features: gpd.GeoDataFrame = gpd.read_file(path)
    if features.crs is None:
        features = features.set_crs(LINK_CRS)
    features = features.to_crs(PLOT_CRS)
    for column in ("highway", "building"):
        if column not in features:
            features[column] = None
    lines: gpd.GeoDataFrame = features[
        features.geom_type.isin(["LineString", "MultiLineString"])
    ]
    # OSM highway tags drawn as roads, and as footpaths.
    roads: gpd.GeoDataFrame = lines[
        lines["highway"].isin(
            [
                "motorway",
                "trunk",
                "primary",
                "secondary",
                "tertiary",
                "unclassified",
                "residential",
                "service",
                "living_street",
            ]
        )
    ]
    paths: gpd.GeoDataFrame = lines[
        lines["highway"].isin(
            [
                "footway",
                "path",
                "pedestrian",
                "steps",
                "cycleway",
                "bridleway",
                "track",
            ]
        )
    ]
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
    minx, _, maxx, maxy = lots.total_bounds
    left = minx - padding
    right = maxx + padding
    bottom = south
    top = maxy + padding
    width = right - left
    height = top - bottom
    box: Bbox = ax.get_position(original=True)
    # The axes are on the page figure, which render() makes PAGE_SIZE.
    figure_width, figure_height = PAGE_SIZE
    box_ratio = (box.height * figure_height) / (box.width * figure_width)
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
            facecolor=Color.building_fill,
            edgecolor=Color.building_edge,
            linewidth=0.4,
            zorder=1,
        )
    if not paths.empty:
        paths.plot(
            ax=ax, color=Color.path, linewidth=0.4, linestyle=(0, (2, 1)), zorder=2
        )
    if not roads.empty:
        roads.plot(ax=ax, color=Color.road, linewidth=0.8, zorder=2)


def load_poster_labels(path: Path) -> gpd.GeoDataFrame:
    """Read building labels placed by buildings.py, with names for the map."""
    labels: pd.DataFrame = pd.read_csv(path, keep_default_na=False)
    return gpd.GeoDataFrame(
        {"name": labels["index_name"].map(map_name)},
        geometry=gpd.points_from_xy(labels["easting"], labels["northing"]),
        crs=PLOT_CRS,
    )


def map_name(name: str) -> str:
    """Shorten a building name for the map and wrap it if it's long.

    Drops the word "Building" (or "Bldg"), then breaks a name over 20
    characters before its last word. Short last words like "2" stay with the
    word before them.

    "Physical Sciences Building" -> "Physical Sciences"
    "Baytree Bookstore/Building" -> "Baytree Bookstore"
    "Press Bldg" -> "Press"
    "Science & Engineering Library" -> "Science & Engineering\\nLibrary"
    "Eloise Pickard Smith Gallery" -> "Eloise Pickard Smith\\nGallery"
    "Natural Sciences 2" -> "Natural Sciences 2" (short enough)
    """
    # Also drops any "/" joining the word to the name.
    name = re.sub(r"\s*/?\b(Building|Bldg)\b", "", name, flags=re.I).strip(" /")
    if len(name) <= 20:
        return name
    words: list[str] = name.split()
    tail = 2 if len(words[-1]) <= 2 and len(words) > 2 else 1
    if len(words) <= tail:
        return name
    return f"{' '.join(words[:-tail])}\n{' '.join(words[-tail:])}"


def draw_building_labels(ax: Axes, labels: gpd.GeoDataFrame, avoid: list[Text]) -> None:
    """Label buildings with their poster names, clear of the lot labels.

    Lot labels stay fixed on their lots, so building labels that collide
    with them (or each other) are nudged instead. Call after the extent is
    set, since collisions are measured in display space.
    """
    texts: list[Text] = []
    for name, x, y in zip(labels["name"], labels.geometry.x, labels.geometry.y):
        texts.append(
            ax.text(
                x,
                y,
                name,
                fontsize=LABEL_FONT_SIZE,
                fontweight="bold",
                color=Color.building_text,
                ha="center",
                va="center",
                # Above lot fills (4), below lot labels and their halos (5.5-6).
                zorder=5,
                clip_on=True,
            )
        )
    separate_labels(ax, texts, avoid)


def text_boxes(texts: list[Text]) -> list[Bbox]:
    """Return each text's bounding box in display coordinates."""
    # With no renderer given, each text uses its figure's.
    return [text.get_window_extent() for text in texts]


def obstacle_points(ax: Axes, boxes: list[Bbox]) -> tuple[list[float], list[float]]:
    """Return a grid of points covering each box, in data coordinates.

    adjustText's objects option converts coordinates twice, which puts
    obstacles far off the map, so obstacles are passed as points instead.
    """
    to_data: Transform = ax.transData.inverted()
    xs: list[float] = []
    ys: list[float] = []
    for box in boxes:
        for fraction_x in np.linspace(0, 1, OBSTACLE_GRID):
            for fraction_y in np.linspace(0, 1, OBSTACLE_GRID):
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
    boxes: list[Bbox] = text_boxes(texts)
    fixed_boxes: list[Bbox] = text_boxes(avoid)
    crowded: list[int] = [
        index
        for index, box in enumerate(boxes)
        if any(box.overlaps(other) for other in fixed_boxes)
        or any(box.overlaps(other) for j, other in enumerate(boxes) if j != index)
    ]
    if not crowded:
        return
    fixed_boxes += [box for index, box in enumerate(boxes) if index not in crowded]
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
        # Padding around each label (x, y).
        expand=(1.1, 1.4),
        # Push harder off the fixed labels than the default.
        force_static=(0.5, 1.0),
        ensure_inside_axes=True,
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
        color=Color.halo,
        alpha=0.75,
        # Outline width in points.
        path_effects=[withStroke(linewidth=1.5, foreground=Color.halo)],
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
    for lot, x, y, longitude, latitude in zip(
        lots.to_dict("records"), anchors.x, anchors.y, links.x, links.y
    ):
        url = maps_url(longitude, latitude)
        label = ax.text(
            x,
            y,
            f"Lot {lot['LOT']} ({lot['PERMIT_SPACES']})",
            fontsize=LABEL_FONT_SIZE,
            fontweight="normal",
            color=Color.lot_text,
            ha="center",
            va="bottom",
            url=url,
            zorder=6,
            clip_on=True,
        )
        info = ax.annotate(
            shorten_enforcement(lot.get("ENFORCEMENT")),
            xy=(0.5, 0),
            xycoords=label,
            xytext=(0, -0.5),
            textcoords="offset points",
            fontsize=4,
            color=Color.lot_text,
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
    movement = {
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
        # Padding around each label (x, y); the y padding leaves room for the
        # enforcement line.
        expand=(1.05, 2.2),
        only_move=movement,
        ensure_inside_axes=True,
        arrowprops={"arrowstyle": "-", "color": Color.lot_edge, "linewidth": 0.3},
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
    # Fill lot polygons by size.
    lots.plot(
        ax=ax,
        color=[lot_color(int(spaces)) for spaces in lots["PERMIT_SPACES"]],
        edgecolor=Color.lot_edge,
        linewidth=0.5,
        zorder=4,
    )
    extent: Polygon = set_extent(ax, lots, padding, south)
    # Only label what's on the map; adjustText would pull others onto the page.
    lot_texts: list[Text] = draw_lot_labels(
        ax, lots[lots.representative_point().within(extent)]
    )
    visible: gpd.GeoDataFrame = building_labels[building_labels.within(extent)]
    draw_building_labels(ax, visible, lot_texts)


def draw_header(figure: Figure, ax: Axes) -> None:
    """Add the title, date (top right), tap hint, and lot size legend."""
    top = 1 - MARGIN / PAGE_SIZE[1]
    left = MARGIN / PAGE_SIZE[0]
    right = 1 - left
    figure.text(left, top, TITLE, fontsize=14, weight="bold", va="top")
    title_baseline = top - 14 / 72 / PAGE_SIZE[1]
    figure.text(
        right,
        title_baseline,
        # Sep 23, 2026
        date.today().strftime("%b %-d, %Y"),
        fontsize=9,
        color=Color.date,
        ha="right",
        va="baseline",
    )
    figure.text(
        left,
        top - 0.022,
        "tap to open in Google Maps",
        fontsize=7,
        color=Color.tap_hint,
        va="top",
    )
    # First entry is a key to the lot labels: no swatch, just sample text.
    # It explains the number in parentheses on lot labels.
    key = Patch(facecolor="none", edgecolor="none", label="Lot ___ (spaces)")
    handles: list[Patch] = [key] + [
        Patch(facecolor=color, edgecolor=Color.lot_edge, linewidth=0.5, label=label)
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
    legend.get_texts()[0].set_color(Color.lot_text)


def text_size(ax: Axes, text: str) -> tuple[float, float]:
    """Return the width and height, in data units, of scale bar text."""
    probe: Text = ax.text(0, 0, text, fontsize=SCALE_FONT_SIZE)
    box: Bbox = text_boxes([probe])[0].transformed(ax.transData.inverted())
    probe.remove()
    return box.width, box.height


def draw_scale_bar(ax: Axes) -> None:
    """Add a scale bar with walking time in the map's bottom-right corner.

    Alternating black and white segments with end caps, distance ticks below,
    and the walking time to the right. The map is in UTM, so data units are
    meters. Call after the extent is set.
    """
    meters = SCALE_FEET * METERS_PER_FOOT
    # Walking speed of 1.4 m/s.
    minutes = round(meters / 1.4 / 60)
    walk = f"about {minutes} min walk"
    height = meters / 40
    pad = height
    walk_width = text_size(ax, walk)[0]
    tick_height = text_size(ax, "0")[1]
    _, x1 = ax.get_xlim()
    y0, _ = ax.get_ylim()
    # Gap from the map edge: one bar height.
    margin = height
    left = x1 - margin - pad - walk_width - 2 * height - meters
    bottom = y0 + margin + pad + tick_height + 1.5 * height
    segment = meters / SCALE_SEGMENTS
    for index in range(SCALE_SEGMENTS):
        ax.add_patch(
            Rectangle(
                (left + index * segment, bottom),
                segment,
                height,
                facecolor=(
                    Color.scale_bar if index % 2 == 0 else Color.scale_bar_light
                ),
                edgecolor=Color.scale_bar,
                linewidth=0.5,
                zorder=8,
                clip_on=True,
            )
        )
    for end in (left, left + meters):
        ax.plot(
            [end, end],
            [bottom - height, bottom + 2 * height],
            color=Color.scale_bar,
            linewidth=0.8,
            solid_capstyle="butt",
            zorder=8,
            clip_on=True,
        )
    texts: list[Text] = []
    for feet in (0, SCALE_FEET // 2, SCALE_FEET):
        texts.append(
            ax.text(
                left + feet * METERS_PER_FOOT,
                bottom - 1.5 * height,
                f"{feet:,} ft" if feet == SCALE_FEET else f"{feet:,}",
                fontsize=SCALE_FONT_SIZE,
                color=Color.scale_text,
                ha="center",
                va="top",
                zorder=8,
                clip_on=True,
            )
        )
    texts.append(
        ax.text(
            left + meters + 2 * height,
            bottom + height / 2,
            walk,
            fontsize=SCALE_FONT_SIZE,
            color=Color.scale_text,
            ha="left",
            va="center",
            zorder=8,
            clip_on=True,
        )
    )
    # Soft white backing, like the legend's, so paths don't cross the bar.
    to_data = ax.transData.inverted()
    box: Bbox = Bbox.union(text_boxes(texts)).transformed(to_data)
    backing_left = min(box.x0, left) - pad
    backing_bottom = box.y0 - pad
    ax.add_patch(
        Rectangle(
            (backing_left, backing_bottom),
            max(box.x1, left + meters) + pad - backing_left,
            bottom + 2 * height + pad - backing_bottom,
            facecolor=Color.scale_backing,
            edgecolor="none",
            alpha=0.9,
            zorder=7,
            clip_on=True,
        )
    )


def draw_credits(figure: Figure, ax: Axes) -> None:
    """Add data credits in small text just below the map."""
    box = ax.get_position()
    figure.text(
        box.x0,
        # 0.05 inches below the map.
        box.y0 - 0.05 / PAGE_SIZE[1],
        "Basemap: © OpenStreetMap contributors · Parking: UCSC TAPS ·"
        " Buildings: The Center for Integrated Spatial Research, UC Santa Cruz",
        fontsize=5,
        color=Color.credits,
        va="top",
    )


def lot_link_areas(
    ax: Axes, lots: gpd.GeoDataFrame
) -> list[tuple[tuple[float, float, float, float], str]]:
    """Return a tap area and Google Maps URL for each lot on the map.

    Each area is the lot's bounding box plus LINK_MARGIN, in PDF points from
    the page's lower-left corner, clipped to the map. Call after the extent
    and axes position are final.
    """
    x0, x1 = ax.get_xlim()
    y0, y1 = ax.get_ylim()
    anchors = lots.representative_point()
    visible = lots[anchors.within(shapely_box(x0, y0, x1, y1))]
    links = visible.representative_point().to_crs(LINK_CRS)
    to_points = ax.transData + ax.figure.transFigure.inverted()  # data -> page fraction
    page_width = PAGE_SIZE[0] * POINTS_PER_INCH
    page_height = PAGE_SIZE[1] * POINTS_PER_INCH
    frame = ax.get_position()
    areas: list[tuple[tuple[float, float, float, float], str]] = []
    for bounds, longitude, latitude in zip(
        visible.geometry.bounds.itertuples(index=False), links.x, links.y
    ):
        corners: np.ndarray = to_points.transform(
            [(bounds[0], bounds[1]), (bounds[2], bounds[3])]
        )
        left: float = max(corners[0][0], frame.x0) * page_width - LINK_MARGIN
        bottom: float = max(corners[0][1], frame.y0) * page_height - LINK_MARGIN
        right: float = min(corners[1][0], frame.x1) * page_width + LINK_MARGIN
        top: float = min(corners[1][1], frame.y1) * page_height + LINK_MARGIN
        areas.append(((left, bottom, right, top), maps_url(longitude, latitude)))
    return areas


def add_links(
    path: Path, areas: list[tuple[tuple[float, float, float, float], str]]
) -> None:
    """Add a link over each area to the first page of a PDF, in place.

    matplotlib can only attach links to text, so the lot shapes get theirs
    here, after saving. The links have no border, so they're invisible.
    """
    writer = PdfWriter(clone_from=PdfReader(path))
    # Viewers send a tap to the last link listed where areas overlap, so add
    # large lots first and small lots (often inside a big lot's box) last.
    ordered: list[tuple[tuple[float, float, float, float], str]] = sorted(
        areas,
        key=lambda area: -(area[0][2] - area[0][0]) * (area[0][3] - area[0][1]),
    )
    for rect, url in ordered:
        writer.add_annotation(
            page_number=0, annotation=Link(rect=rect, url=url, border=[0, 0, 0])
        )
    # Move the lot areas ahead of the label links matplotlib wrote, so a tap
    # on a label always opens that label's lot, even over a neighbor's area.
    page = writer.pages[0]
    annots = page["/Annots"].get_object()
    if not isinstance(annots, ArrayObject):
        raise ValueError(f"{path}: page annotations are not a list")
    annotations = list(annots)
    added = len(ordered)
    page[NameObject("/Annots")] = ArrayObject(
        annotations[-added:] + annotations[:-added]
    )
    with path.open("wb") as handle:
        writer.write(handle)


def render(
    parking_path: Path, basemap_path: Path, labels_path: Path, output_path: Path
) -> None:
    """Draw the map and write it to a letter-size PDF."""
    lots = load_parking(parking_path)
    roads, paths, buildings = load_basemap(basemap_path)
    building_labels = load_poster_labels(labels_path)
    print(
        f"loaded: {len(lots)} lots, {len(roads)} roads, {len(paths)} paths,"
        f" {len(buildings)} buildings, {len(building_labels)} building labels"
    )

    figure = Figure(figsize=PAGE_SIZE)
    # adjustText measures text through a renderer; savefig still writes PDF.
    FigureCanvasAgg(figure)
    width = 1 - 2 * MARGIN / PAGE_SIZE[0]
    # Leave 0.6 inches above the map for the header.
    height = 1 - (2 * MARGIN + 0.6) / PAGE_SIZE[1]
    ax = figure.add_axes((MARGIN / PAGE_SIZE[0], MARGIN / PAGE_SIZE[1], width, height))

    campus = campus_lots(lots)
    draw_map(
        ax,
        campus,
        roads,
        paths,
        buildings,
        building_labels,
        # Meters of map beyond the outermost lots; labels stay inside the
        # axes anyway.
        40.0,
        south_border(campus),
    )
    draw_header(figure, ax)
    draw_scale_bar(ax)
    draw_credits(figure, ax)
    print(f"drew map: {len(campus)} campus lots")

    areas: list[tuple[tuple[float, float, float, float], str]] = lot_link_areas(
        ax, campus
    )
    figure.savefig(output_path, metadata={"Title": TITLE})
    add_links(output_path, areas)
    print(f"wrote {output_path} with {len(areas)} lot links")
    # Raster copy next to the PDF, same name with .png, for a quick preview.
    preview = output_path.with_suffix(".png")
    figure.savefig(preview, dpi=200)
    print(f"wrote {preview}")


def main() -> None:
    """Parse command line arguments and render the map."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("parking", type=Path, help="parking lot GeoJSON")
    parser.add_argument("basemap", type=Path, help="OSM basemap GeoJSON")
    parser.add_argument("labels", type=Path, help="poster building labels CSV")
    parser.add_argument("output", type=Path, help="PDF to write")
    args = parser.parse_args()
    render(args.parking, args.basemap, args.labels, args.output)


# Embed TrueType fonts so PDF text stays selectable and searchable.
matplotlib.rcParams["pdf.fonttype"] = 42

if __name__ == "__main__":
    main()
