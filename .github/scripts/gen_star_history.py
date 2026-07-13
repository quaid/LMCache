# SPDX-License-Identifier: Apache-2.0
"""Generate light/dark star-history SVG charts for the LMCache README.

This script reads the repository's own stargazer timeline using the GitHub REST
API and renders two self-contained SVG line charts (one tuned for light themes,
one for dark) showing cumulative stars over time. It has **no third-party
dependencies** -- only the Python standard library -- and is intended to run
inside the repository's own GitHub Actions workflow, where the built-in
``GITHUB_TOKEN`` is authorized to read the stargazer timeline (that endpoint is
restricted to a repository's own collaborators/automation).

Usage::

    GITHUB_TOKEN=<token> python .github/scripts/gen_star_history.py \
        --repo LMCache/LMCache --out-dir asset

The generated files are ``<out-dir>/star_history_light.svg`` and
``<out-dir>/star_history_dark.svg``.
"""

# Standard
from dataclasses import dataclass
from datetime import datetime, timezone
import argparse
import json
import math
import os
import sys
import urllib.error
import urllib.request

API_ROOT = "https://api.github.com"
# Number of history samples to draw. The stargazer timeline is sampled evenly
# across pages rather than fully downloaded, matching how star-history.com keeps
# the request count small for large repositories.
MAX_SAMPLES = 24
PER_PAGE = 100
USER_AGENT = "lmcache-star-history-generator"


@dataclass(frozen=True)
class Point:
    """A single point on the cumulative-stars timeline.

    Attributes:
        timestamp: When the ``count``-th star was recorded (UTC).
        count: Cumulative number of stars at ``timestamp``.
    """

    timestamp: datetime
    count: int


@dataclass(frozen=True)
class Palette:
    """Colors for one SVG theme variant.

    Attributes:
        name: Theme name, used in the output file suffix (``light``/``dark``).
        axis: Stroke color for axes and gridlines.
        text: Fill color for labels and the title.
        line: Stroke color for the star-count curve.
        area: Fill color (with alpha) for the area under the curve.
    """

    name: str
    axis: str
    text: str
    line: str
    area: str


LIGHT = Palette(
    name="light", axis="#d0d7de", text="#57606a", line="#2f81f7", area="#2f81f733"
)
DARK = Palette(
    name="dark", axis="#30363d", text="#8b949e", line="#58a6ff", area="#58a6ff33"
)

# Chart geometry (SVG user units).
WIDTH = 800
HEIGHT = 420
MARGIN_LEFT = 72
MARGIN_RIGHT = 28
MARGIN_TOP = 52
MARGIN_BOTTOM = 52


def _request(url: str, token: str) -> tuple[list, dict[str, str]]:
    """Perform an authenticated GitHub API GET returning JSON and headers.

    Args:
        url: Fully-qualified GitHub API URL.
        token: GitHub token sent as a Bearer credential.

    Returns:
        A tuple of the decoded JSON body and the response headers.

    Raises:
        urllib.error.HTTPError: If the API responds with a non-2xx status.
    """
    req = urllib.request.Request(url)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github.star+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", USER_AGENT)
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.loads(resp.read().decode("utf-8"))
        headers = {k.lower(): v for k, v in resp.headers.items()}
    return body, headers


def _total_stars(repo: str, token: str) -> int:
    """Return the repository's current stargazer count.

    Args:
        repo: Repository in ``owner/name`` form.
        token: GitHub token.

    Returns:
        The current number of stargazers.
    """
    body, _ = _request(f"{API_ROOT}/repos/{repo}", token)
    return int(body["stargazers_count"])


def _parse_starred_at(raw: str) -> datetime:
    """Parse a GitHub ``starred_at`` ISO-8601 timestamp into an aware datetime.

    Args:
        raw: Timestamp such as ``2025-08-01T12:00:00Z``.

    Returns:
        A timezone-aware :class:`datetime` in UTC.
    """
    return datetime.fromisoformat(raw.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_timeline(repo: str, token: str) -> list[Point]:
    """Sample the cumulative-stars timeline for ``repo``.

    The stargazer list is paginated at :data:`PER_PAGE` entries per page. Rather
    than downloading every page, up to :data:`MAX_SAMPLES` pages are sampled
    evenly across the full range; the first entry of each sampled page yields a
    ``(timestamp, cumulative_count)`` point. The current total is appended as the
    final point.

    Args:
        repo: Repository in ``owner/name`` form.
        token: GitHub token authorized to read the stargazer timeline.

    Returns:
        Timeline points sorted by timestamp. Contains at least the final total.

    Raises:
        ValueError: If the repository reports zero stars.
    """
    total = _total_stars(repo, token)
    if total <= 0:
        raise ValueError(f"{repo} has no stars to chart")

    total_pages = max(1, math.ceil(total / PER_PAGE))
    if total_pages <= MAX_SAMPLES:
        pages = list(range(1, total_pages + 1))
    else:
        step = total_pages / MAX_SAMPLES
        pages = sorted({max(1, round(i * step)) for i in range(MAX_SAMPLES)})

    points: list[Point] = []
    for page in pages:
        url = f"{API_ROOT}/repos/{repo}/stargazers?per_page={PER_PAGE}&page={page}"
        body, _ = _request(url, token)
        if not body:
            continue
        first = body[0]
        count = (page - 1) * PER_PAGE + 1
        points.append(Point(_parse_starred_at(first["starred_at"]), count))

    points.append(Point(datetime.now(timezone.utc), total))
    points.sort(key=lambda p: p.timestamp)
    return points


def _format_count(value: int) -> str:
    """Format a star count compactly (e.g. ``10200`` -> ``10.2k``).

    Args:
        value: A non-negative star count.

    Returns:
        A short human-readable label.
    """
    if value >= 1000:
        scaled = value / 1000
        text = f"{scaled:.1f}".rstrip("0").rstrip(".")
        return f"{text}k"
    return str(value)


def _nice_ceiling(value: int) -> int:
    """Round ``value`` up to a visually tidy axis maximum.

    Args:
        value: The largest data value on the axis.

    Returns:
        A rounded-up bound (1/2/5 x power of ten) not below ``value``.
    """
    if value <= 0:
        return 1
    magnitude = 10 ** (len(str(value)) - 1)
    for factor in (1, 2, 5, 10):
        candidate = factor * magnitude
        if candidate >= value:
            return candidate
    return 10 * magnitude


def _escape(text: str) -> str:
    """Escape text for safe inclusion in SVG markup.

    Args:
        text: Raw label text.

    Returns:
        Text with XML metacharacters escaped.
    """
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def render_svg(points: list[Point], repo: str, palette: Palette) -> str:
    """Render the cumulative-stars timeline as a standalone SVG document.

    Args:
        points: Timeline points sorted by timestamp (see :func:`fetch_timeline`).
        repo: Repository in ``owner/name`` form, shown in the title.
        palette: Theme colors to draw with.

    Returns:
        A complete SVG document as a string.
    """
    plot_w = WIDTH - MARGIN_LEFT - MARGIN_RIGHT
    plot_h = HEIGHT - MARGIN_TOP - MARGIN_BOTTOM
    t_min = points[0].timestamp.timestamp()
    t_max = points[-1].timestamp.timestamp()
    t_span = max(1.0, t_max - t_min)
    c_max = _nice_ceiling(points[-1].count)

    def x_of(ts: datetime) -> float:
        return MARGIN_LEFT + (ts.timestamp() - t_min) / t_span * plot_w

    def y_of(count: int) -> float:
        return MARGIN_TOP + plot_h - (count / c_max) * plot_h

    coords = [(x_of(p.timestamp), y_of(p.count)) for p in points]
    line_pts = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
    baseline = MARGIN_TOP + plot_h
    area_pts = (
        f"{coords[0][0]:.1f},{baseline:.1f} "
        + line_pts
        + f" {coords[-1][0]:.1f},{baseline:.1f}"
    )

    parts: list[str] = []
    parts.append(
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{WIDTH}" '
        f'height="{HEIGHT}" viewBox="0 0 {WIDTH} {HEIGHT}" '
        f'font-family="-apple-system,Segoe UI,Helvetica,Arial,sans-serif">'
    )
    parts.append(
        f'<text x="{MARGIN_LEFT}" y="30" fill="{palette.text}" '
        f'font-size="18" font-weight="600">{_escape(repo)} ⭐ star history</text>'
    )

    # Horizontal gridlines + y-axis labels.
    for i in range(5):
        value = round(c_max * i / 4)
        y = y_of(value)
        parts.append(
            f'<line x1="{MARGIN_LEFT}" y1="{y:.1f}" x2="{MARGIN_LEFT + plot_w}" '
            f'y2="{y:.1f}" stroke="{palette.axis}" stroke-width="1"/>'
        )
        parts.append(
            f'<text x="{MARGIN_LEFT - 10}" y="{y + 4:.1f}" fill="{palette.text}" '
            f'font-size="12" text-anchor="end">{_format_count(value)}</text>'
        )

    # X-axis date labels (up to five evenly spaced points).
    label_count = min(5, len(points))
    seen: set[str] = set()
    for i in range(label_count):
        idx = round(i * (len(points) - 1) / max(1, label_count - 1))
        point = points[idx]
        label = point.timestamp.strftime("%b %y")
        if label in seen:
            continue
        seen.add(label)
        x = x_of(point.timestamp)
        parts.append(
            f'<text x="{x:.1f}" y="{baseline + 20:.1f}" fill="{palette.text}" '
            f'font-size="12" text-anchor="middle">{label}</text>'
        )

    parts.append(f'<polygon points="{area_pts}" fill="{palette.area}" stroke="none"/>')
    parts.append(
        f'<polyline points="{line_pts}" fill="none" stroke="{palette.line}" '
        f'stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>'
    )
    parts.append("</svg>")
    return "\n".join(parts) + "\n"


def main(argv: list[str]) -> int:
    """CLI entry point: fetch the timeline and write both SVG variants.

    Args:
        argv: Command-line arguments (excluding the program name).

    Returns:
        Process exit code (0 on success, non-zero on failure).
    """
    parser = argparse.ArgumentParser(description="Generate star-history SVGs.")
    parser.add_argument(
        "--repo",
        default=os.environ.get("GITHUB_REPOSITORY", "LMCache/LMCache"),
        help="Repository in owner/name form.",
    )
    parser.add_argument(
        "--out-dir", default="asset", help="Directory to write SVG files into."
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("error: GITHUB_TOKEN is not set", file=sys.stderr)
        return 1

    try:
        points = fetch_timeline(args.repo, token)
    except urllib.error.HTTPError as exc:
        print(f"error: GitHub API returned {exc.code}: {exc.reason}", file=sys.stderr)
        return 1
    except (ValueError, urllib.error.URLError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    os.makedirs(args.out_dir, exist_ok=True)
    for palette in (LIGHT, DARK):
        svg = render_svg(points, args.repo, palette)
        path = os.path.join(args.out_dir, f"star_history_{palette.name}.svg")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        print(f"wrote {path} ({len(points)} points)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
