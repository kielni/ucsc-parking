"""Extract building labels from the UCSC Campus Map Poster PDF and place them.

Usage: python buildings.py POSTER BASEMAP OUTPUT

Reads the black map labels (building names), including labels rotated to
follow a building, and joins labels that wrap over several lines. Skips the
white area labels (colleges, fields), curved road names, grid labels, the
index box, and the credits. Each label gets its grid cell (e.g. "E4") and,
when one matches, the properly capitalized name from the poster's index.

The poster has no coordinates, so labels are placed on the OSM buildings in
BASEMAP (the osmium GeoJSON export) through an affine fit from poster points
to map meters. Writes a CSV with one row per label: x and y are the label
center in poster points from the top-left corner, angle is the text
direction in degrees counterclockwise from horizontal, and easting and
northing are the label's map position in UTM zone 10N (EPSG:32610) meters.
"""

import argparse
import math
import re
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
from pathlib import Path

import geopandas as gpd
import numpy as np
import pandas as pd
import pdfplumber
from pdfplumber.page import Page

# CMYK fill colors used on the poster.
WHITE = (0.0, 0.0, 0.0, 0.0)
GRID_COLOR = (0.75, 0.68, 0.67, 0.9)

# The index and legend box in the lower-left corner: (x0, top, x1, bottom).
INDEX_BOX = (80.0, 2120.0, 890.0, 2510.0)
# Text below this (points from the top) is credits and grid letters.
MAP_BOTTOM = 2510.0
GRID_REF: re.Pattern[str] = re.compile(r"^[A-J]\d{1,2}$")

# Map labels ending in one of these words are road names, not buildings.
ROAD_WORDS = {
    "COURT",
    "DRIVE",
    "GRADE",
    "LANE",
    "PATH",
    "ROAD",
    "STREET",
    "TRAIL",
    "WAY",
}
# Labels containing one of these words are parking lots, not buildings.
PARKING_WORDS = {"PARKING"}
# Fragments of road names that the road and curve checks miss.
NOT_BUILDINGS = {"BAY D"}

# Title casing for labels with no index name: these words stay lowercase
# (unless first) and these stay uppercase.
SMALL_WORDS = {"a", "and", "for", "of", "the"}
ACRONYMS = {"KZSC", "NS", "OPERS", "UCO"}

# Characters continue a line when their direction differs by less than this
# (degrees) and they start within this distance (x font size) of where the
# previous character ended. Road names on curves turn a little every letter.
ANGLE_TOLERANCE = 1.0
ADVANCE_TOLERANCE = 0.3
# A following letter turned by less than this (degrees), within this distance
# (x font size), marks curved text; the gap widens where road names bend.
CURVE_ANGLE = 30.0
CURVE_ADVANCE = 1.0
# Stacked lines of one label are this far apart (x font size).
LINE_GAP = (0.5, 1.4)
# Index entries in different columns are at least this far apart (points).
COLUMN_GAP = 12.0
# Short forms used on map labels or in the index, expanded before matching.
ABBREVIATIONS = {
    "APTS": "APARTMENTS",
    "BLDG": "BUILDING",
    "LABS": "LABORATORIES",
}
# Minimum similarity for a label to take its name from the index.
MATCH_CUTOFF = 0.8


@dataclass
class Line:
    """One line of a map label, in PDF coordinates (y up)."""

    text: str
    start: tuple[float, float]
    end: tuple[float, float]
    direction: tuple[float, float]
    size: float
    style: tuple[str, float, tuple[float, ...]]
    curved: bool = False

    @property
    def center(self) -> tuple[float, float]:
        """Return the midpoint of the baseline."""
        return (
            (self.start[0] + self.end[0]) / 2,
            (self.start[1] + self.end[1]) / 2,
        )

    @property
    def length(self) -> float:
        """Return the length of the baseline."""
        return math.dist(self.start, self.end)


@dataclass
class Label:
    """A building label on the map, with its grid cell and index match."""

    text: str
    x: float
    y: float
    angle: float
    size: float
    grid: str
    index_name: str = ""


def cmyk(color: object) -> tuple[float, ...]:
    """Round a pdfplumber color to 2 places for comparison."""
    if isinstance(color, (list, tuple)):
        return tuple(round(float(c), 2) for c in color)
    return ()


def in_index_box(obj: dict) -> bool:
    """Return True if a character or word lies inside the index box."""
    x0, top, x1, bottom = INDEX_BOX
    return x0 <= obj["x0"] and obj["x1"] <= x1 and top <= obj["top"] <= bottom


def is_label_char(char: dict) -> bool:
    """Return True for characters that can be part of a building label."""
    return (
        cmyk(char["non_stroking_color"]) not in (WHITE, GRID_COLOR)
        and not in_index_box(char)
        and char["top"] < MAP_BOTTOM
    )


def char_geometry(
    char: dict,
) -> tuple[float, tuple[float, float], tuple[float, float]]:
    """Return a character's font size, baseline direction, and origin."""
    a, b, _, _, e, f = char["matrix"]
    size = math.hypot(a, b)
    return size, (a / size, b / size), (e, f)


def build_lines(chars: list[dict]) -> list[Line]:
    """Join characters, in content-stream order, into baseline-aligned lines."""
    lines: list[Line] = []
    current: Line | None = None
    for char in chars:
        size, direction, origin = char_geometry(char)
        style: tuple[str, float, tuple[float, ...]] = (
            char["fontname"],
            round(size, 1),
            cmyk(char["non_stroking_color"]),
        )
        end: tuple[float, float] = (
            origin[0] + char["adv"] * size * direction[0],
            origin[1] + char["adv"] * size * direction[1],
        )
        turn = 180.0
        gap = math.inf
        if current is not None and current.style == style:
            turn = angle_between(current.direction, direction)
            gap = math.dist(current.end, origin)
        if (
            current is not None
            and gap < ADVANCE_TOLERANCE * size
            and turn < ANGLE_TOLERANCE
        ):
            current.text += char["text"]
            current.end = end
            continue
        # A letter that follows on but turns slightly means text on a curve.
        curved: bool = gap < CURVE_ADVANCE * size and turn < CURVE_ANGLE
        if curved and current is not None:
            current.curved = True
        current = Line(char["text"], origin, end, direction, size, style, curved)
        lines.append(current)
    return [line for line in lines if line.text.strip()]


def angle_between(first: tuple[float, float], second: tuple[float, float]) -> float:
    """Return the angle in degrees between two unit vectors."""
    dot = first[0] * second[0] + first[1] * second[1]
    return math.degrees(math.acos(max(-1.0, min(1.0, dot))))


def stacks(upper: Line, lower: Line) -> bool:
    """Return True if lower is the next line of the same label as upper."""
    if upper.style != lower.style:
        return False
    dot = (
        upper.direction[0] * lower.direction[0]
        + upper.direction[1] * lower.direction[1]
    )
    if dot < math.cos(math.radians(ANGLE_TOLERANCE)):
        return False
    dx = lower.center[0] - upper.center[0]
    dy = lower.center[1] - upper.center[1]
    along = dx * upper.direction[0] + dy * upper.direction[1]
    # Distance below the upper line, measured perpendicular to its baseline.
    below = dx * upper.direction[1] - dy * upper.direction[0]
    if not LINE_GAP[0] * upper.size <= below <= LINE_GAP[1] * upper.size:
        return False
    # Label lines are centered, so their centers are close together.
    return abs(along) <= max(upper.length, lower.length) / 2


def group_lines(lines: list[Line]) -> list[list[Line]]:
    """Group stacked lines into labels, top line first."""
    groups: list[list[Line]] = []
    for line in lines:
        group: list[Line]
        for group in reversed(groups):
            if stacks(group[-1], line):
                group.append(line)
                break
        else:
            groups.append([line])
    return groups


def is_building(text: str) -> bool:
    """Drop road names, parking lots, stray letters, and numbers."""
    words = text.split()
    if words[-1] in ROAD_WORDS or PARKING_WORDS & set(words):
        return False
    if text in NOT_BUILDINGS:
        return False
    letters = sum(1 for char in text if char.isalpha())
    return letters >= 4 and any(len(word) >= 3 and word.isalpha() for word in words)


def extract_words(page: Page) -> list[dict]:
    """Extract horizontal words with their fill color."""
    return page.extract_words(
        extra_attrs=["non_stroking_color"], x_tolerance=1.5, y_tolerance=1
    )


def grid_axes(page: Page) -> tuple[dict[str, float], dict[str, float]]:
    """Return grid column centers (A-J by x) and row centers (1-15 by y)."""
    columns: dict[str, float] = {}
    rows: dict[str, float] = {}
    for word in extract_words(page):
        if cmyk(word["non_stroking_color"]) != GRID_COLOR or in_index_box(word):
            continue
        center_x: float = (word["x0"] + word["x1"]) / 2
        center_y: float = (word["top"] + word["bottom"]) / 2
        if word["text"].isalpha() and len(word["text"]) == 1:
            columns[word["text"]] = center_x
        elif word["text"].isdigit():
            rows[word["text"]] = center_y
    return columns, rows


def grid_cell(
    x: float, y: float, columns: dict[str, float], rows: dict[str, float]
) -> str:
    """Return the grid cell name (e.g. "E4") nearest to a poster position."""
    column = min(columns, key=lambda name: abs(columns[name] - x))
    row = min(rows, key=lambda name: abs(rows[name] - y))
    return f"{column}{row}"


def extract_index(page: Page) -> list[tuple[str, str]]:
    """Return (name, grid cell) entries from the poster's index."""
    words = [word for word in extract_words(page) if in_index_box(word)]
    entries: list[tuple[str, str]] = []
    name: list[str] = []
    last: dict | None = None
    for word in sorted(words, key=lambda w: (round(w["top"]), w["x0"])):
        new_line: bool = last is None or abs(word["top"] - last["top"]) > 1
        if new_line:
            name = []
        if GRID_REF.match(word["text"]):
            # Grid cells are right-aligned in their own column after the name.
            if name:
                entries.append((" ".join(name), word["text"]))
            name = []
        elif (
            name
            and last is not None
            and (word["x0"] - last["x1"] > COLUMN_GAP or word["x0"] < last["x0"])
        ):
            # A wide gap, or a jump back left from a word a fraction of a
            # point higher, starts a new column entry.
            name = [word["text"]]
        else:
            name.append(word["text"])
        last = word
    return entries


def normalize(text: str) -> str:
    """Uppercase, expand abbreviations, and strip punctuation and spaces."""
    words = re.findall(r"[A-Z0-9]+", text.upper())
    return "".join(ABBREVIATIONS.get(word, word) for word in words)


def title_case(text: str) -> str:
    """Title-case an uppercase label for use as a name.

    Unlike str.title, this keeps "2ND" as "2nd" and "WOMEN'S" as "Women's".

    >>> title_case("WOMEN'S CENTER")
    "Women's Center"
    >>> title_case("2ND STAGE")
    '2nd Stage'
    >>> title_case("CENTER FOR ADAPTIVE OPTICS")
    'Center for Adaptive Optics'
    >>> title_case("NS 2 ANNEX")
    'NS 2 Annex'
    """
    words: list[str] = []
    for index, word in enumerate(text.split()):
        if word in ACRONYMS:
            words.append(word)
        elif index > 0 and word.lower() in SMALL_WORDS:
            words.append(word.lower())
        else:
            words.append(word[0] + word[1:].lower())
    return " ".join(words)


def near_cell(first: str, second: str) -> bool:
    """Return True if two grid cells are the same or touch (e.g. C6 and D7)."""
    columns = abs(ord(first[0]) - ord(second[0]))
    rows = abs(int(first[1:]) - int(second[1:]))
    return columns <= 1 and rows <= 1


def match_index(label: Label, index: list[tuple[str, str]]) -> str:
    """Return the best-matching nearby index name, preferring the same cell.

    Index cells are approximate, so neighboring cells also count; names from
    farther away are a different building with a similar name.
    """
    target: str = normalize(label.text)
    best_name = ""
    best_score = MATCH_CUTOFF
    for name, cell in index:
        if not near_cell(cell, label.grid):
            continue
        score: float = SequenceMatcher(None, target, normalize(name)).ratio()
        if cell == label.grid:
            score += 0.05
        if score > best_score:
            best_name, best_score = name, score
    return best_name


def make_label(
    group: list[Line],
    height: float,
    columns: dict[str, float],
    rows: dict[str, float],
) -> Label:
    """Build a label from its lines, converting to top-left coordinates."""
    first: Line = group[0]
    last: Line = group[-1]
    # Midway between the first and last baselines, raised half a cap height.
    x = (first.center[0] + last.center[0]) / 2 - first.direction[1] * (
        first.size * 0.35
    )
    y = height - (
        (first.center[1] + last.center[1]) / 2 + first.direction[0] * first.size * 0.35
    )
    return Label(
        text=" ".join(line.text.strip() for line in group),
        x=round(x, 1),
        y=round(y, 1),
        angle=round(math.degrees(math.atan2(first.direction[1], first.direction[0]))),
        size=round(first.size, 1),
        grid=grid_cell(x, y, columns, rows),
    )


def extract_labels(poster: Path) -> list[Label]:
    """Extract building labels from the poster, matched to its index."""
    with pdfplumber.open(poster) as pdf:
        page: Page = pdf.pages[0].dedupe_chars()
        columns: dict[str, float]
        rows: dict[str, float]
        columns, rows = grid_axes(page)
        index: list[tuple[str, str]] = extract_index(page)
        chars: list[dict] = [char for char in page.chars if is_label_char(char)]
        height = float(page.height)

    labels: list[Label] = []
    group: list[Line]
    for group in group_lines(build_lines(chars)):
        if any(line.curved for line in group):
            continue
        label = make_label(group, height, columns, rows)
        if not is_building(label.text):
            continue
        label.index_name = match_index(label, index) or title_case(label.text)
        labels.append(label)
    return labels


def load_buildings(path: Path) -> gpd.GeoDataFrame:
    """Read OSM building outlines from the basemap, in UTM zone 10N meters."""
    features = gpd.read_file(path)
    if features.crs is None:
        features = features.set_crs("EPSG:4326")
    # Same CRS as main.py's PLOT_CRS, so the map can use the positions as is.
    features = features.to_crs("EPSG:32610")
    for column in ("building", "name"):
        if column not in features:
            features[column] = None
    return features[
        features["building"].notna()
        & features.geom_type.isin(["Polygon", "MultiPolygon"])
    ]


def named_buildings(buildings: gpd.GeoDataFrame) -> gpd.GeoSeries:
    """Return OSM building outlines by normalized name.

    Buildings split into several polygons with the same name are merged.
    """
    named = buildings[buildings["name"].notna()].copy()
    named["key"] = named["name"].map(normalize)
    return named.dissolve(by="key").geometry


def fit_poster_transform(
    labels: pd.DataFrame, buildings: gpd.GeoDataFrame
) -> np.ndarray:
    """Fit an affine transform from poster points to map meters.

    Control points are poster labels whose name matches exactly one OSM
    building name; buildings split into several polygons are merged first.
    The worst control point is dropped until all fit within 40 m, since a
    few poster labels sit beside their building. Fails if fewer than 6
    control points remain. Returns a 3x2 matrix:
    [x, y, 1] @ matrix = [easting, northing].
    """
    anchors = named_buildings(buildings).representative_point()
    poster = labels.assign(key=labels["index_name"].map(normalize))
    poster = poster.drop_duplicates("key", keep=False)
    pairs = poster[poster["key"].isin(anchors.index)]
    source = np.column_stack([pairs["x"], pairs["y"], np.ones(len(pairs))])
    target = np.column_stack([anchors.x.loc[pairs["key"]], anchors.y.loc[pairs["key"]]])
    while len(source) >= 6:
        matrix = np.linalg.lstsq(source, target, rcond=None)[0]
        errors = np.linalg.norm(source @ matrix - target, axis=1)
        if errors.max() <= 40.0:
            print(
                f"poster fit: {len(source)} control points,"
                f" RMS {np.sqrt(np.mean(errors**2)):.1f} m"
            )
            return matrix
        worst = int(errors.argmax())
        source = np.delete(source, worst, axis=0)
        target = np.delete(target, worst, axis=0)
    raise ValueError("too few poster labels match OSM building names")


def place_labels(labels: pd.DataFrame, buildings: gpd.GeoDataFrame) -> pd.DataFrame:
    """Add each label's map position, on its building where possible.

    A label whose name matches one compact OSM building (all parts within a
    150 m diagonal) goes on that building. Other labels are placed by the
    poster fit and snapped to the nearest building within 25 m. Returns the
    labels with easting and northing columns (meters, to 0.1 m).
    """
    matrix = fit_poster_transform(labels, buildings)
    source = np.column_stack([labels["x"], labels["y"], np.ones(len(labels))])
    placed = source @ matrix
    points = gpd.GeoDataFrame(
        geometry=gpd.points_from_xy(placed[:, 0], placed[:, 1]),
        index=labels.index,
        crs=buildings.crs,
    )
    nearest = gpd.sjoin_nearest(
        points, buildings[["geometry"]], how="left", max_distance=25.0
    )
    # Ties return several rows per label; keep the first building.
    nearest = nearest[~nearest.index.duplicated()]
    snapped = nearest["index_right"].dropna()
    centers = buildings.representative_point()
    points.loc[snapped.index, "geometry"] = centers.loc[snapped].values

    # Same-named buildings spread across campus (e.g. several Dining Commons)
    # are too ambiguous to place a label by name.
    outlines = named_buildings(buildings)
    bounds = outlines.bounds
    diagonal = np.hypot(
        bounds["maxx"] - bounds["minx"], bounds["maxy"] - bounds["miny"]
    )
    # Center point of each compact building, by name.
    compact = outlines.representative_point().loc[diagonal <= 150.0]
    keys = labels["index_name"].map(normalize)
    unique = ~keys.duplicated(keep=False)
    matched = keys[unique & keys.isin(compact.index)]
    points.loc[matched.index, "geometry"] = compact.loc[matched].values
    return labels.assign(
        easting=points.geometry.x.round(1), northing=points.geometry.y.round(1)
    )


def main() -> None:
    """Parse command line arguments and write the building label CSV."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("poster", type=Path, help="campus map poster PDF")
    parser.add_argument("basemap", type=Path, help="OSM basemap GeoJSON")
    parser.add_argument("output", type=Path, help="CSV to write")
    args = parser.parse_args()
    labels = pd.DataFrame([asdict(label) for label in extract_labels(args.poster)])
    placed = place_labels(labels, load_buildings(args.basemap))
    # Sorted by grid cell then name, so the CSV reads like the poster index.
    columns = [
        "text",
        "index_name",
        "grid",
        "x",
        "y",
        "angle",
        "size",
        "easting",
        "northing",
    ]
    placed.sort_values(["grid", "text"]).to_csv(
        args.output, columns=columns, index=False
    )
    print(f"{args.output}: {len(placed)} labels")


if __name__ == "__main__":
    main()
