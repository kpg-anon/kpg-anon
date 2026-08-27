#!/usr/bin/env python3
"""Generate a terminal-style stat card for a GitHub profile README.

Pulls repository, language and contribution data from the GitHub GraphQL API,
converts the account avatar to coloured ASCII, and writes dark_mode.svg and
light_mode.svg. Every run of characters is placed at an absolute x with an
explicit textLength, so columns line up no matter which monospace font the
viewing browser falls back to.

The card is laid out landscape (avatar and stats on the left, language bars on
the right) so it does not stretch the profile page vertically. The contribution
calendar is deliberately absent: GitHub already draws one above the README.
"""

import argparse
import datetime as dt
import io
import json
import math
import os
import sys
import urllib.request
from collections import Counter

API = "https://api.github.com/graphql"

# Layout, in character cells. FONT_W is the assumed advance width; textLength
# forces the real width to match it, so the value only sets the overall scale.
FONT_SIZE = 14.0
FONT_W = 8.4
LINE_H = 19.0
PAD_X = 22.0
CHROME_H = 34.0
PAD_TOP = 16.0
PAD_BOTTOM = 18.0

# The avatar is drawn on its own half-scale grid, so it gets twice the
# resolution in the same space. LINE_H / FONT_W is the cell aspect ratio, so
# these numbers keep a square image square.
AVATAR_COLS = 68
AVATAR_ROWS = 30
AVATAR_SCALE = 0.5

INFO_COL = int(AVATAR_COLS * AVATAR_SCALE) + 3      # 37
INFO_KEY_W = 13
LANG_COL = 85
BAR_CELLS = 18
COLS = 137

FONT_STACK = ("ui-monospace, SFMono-Regular, Menlo, Consolas, "
              "DejaVu Sans Mono, Liberation Mono, monospace")

THEMES = {
    "dark": {
        "bg": "#0d1117", "chrome": "#161b22", "border": "#30363d",
        "fg": "#c9d1d9", "dim": "#6e7681", "title": "#8b949e",
        "key": "#58a6ff", "accent": "#3fb950", "track": "#21262d",
        "dots": ["#ff5f56", "#ffbd2e", "#27c93f"],
        "lang_lum": (0.30, 1.0), "avatar_lum": (0.0, 1.0),
    },
    "light": {
        "bg": "#ffffff", "chrome": "#f6f8fa", "border": "#d0d7de",
        "fg": "#1f2328", "dim": "#59636e", "title": "#59636e",
        "key": "#0969da", "accent": "#1a7f37", "track": "#eaeef2",
        "dots": ["#ff5f56", "#ffbd2e", "#27c93f"],
        "lang_lum": (0.0, 0.62), "avatar_lum": (0.0, 0.52),
    },
}

FALLBACK_LANG_COLORS = ["#58a6ff", "#3fb950", "#d29922", "#f778ba",
                        "#a371f7", "#ff7b72", "#39c5cf", "#db6d28"]


# --------------------------------------------------------------------------
# Colour
# --------------------------------------------------------------------------

def hex_to_rgb(value):
    value = value.lstrip("#")
    if len(value) == 3:
        value = "".join(c * 2 for c in value)
    return tuple(int(value[i:i + 2], 16) for i in (0, 2, 4))


def rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(round(c)))) for c in rgb)


def luminance(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def fit_contrast(color, bounds):
    """Nudge a colour into a luminance range so it stays visible on the theme.

    Some language colours sit at one end of the range and vanish against one of
    the two backgrounds: Lua is #000080, nearly invisible on #0d1117, and the
    avatar skin tones wash out against #ffffff.
    """
    low, high = bounds
    rgb = hex_to_rgb(color)
    for _ in range(12):
        lum = luminance(rgb)
        if lum < low:
            rgb = tuple(c + (255 - c) * 0.18 for c in rgb)
        elif lum > high:
            rgb = tuple(c * 0.82 for c in rgb)
        else:
            break
    return rgb_to_hex(rgb)


# --------------------------------------------------------------------------
# GitHub API
# --------------------------------------------------------------------------

def gql(query, variables, token):
    body = json.dumps({"query": query, "variables": variables}).encode("utf-8")
    req = urllib.request.Request(API, data=body, headers={
        "Authorization": "bearer " + token,
        "Content-Type": "application/json",
        "User-Agent": "profile-card-generator",
    })
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.load(resp)
    if payload.get("errors"):
        raise SystemExit("GraphQL error:\n" +
                         json.dumps(payload["errors"], indent=2))
    return payload["data"]


PROFILE_QUERY = """
query($login: String!, $cursor: String) {
  user(login: $login) {
    login name avatarUrl location createdAt
    followers { totalCount }
    following { totalCount }
    repositories(first: 100, after: $cursor, ownerAffiliations: OWNER,
                 isFork: false, privacy: PUBLIC,
                 orderBy: {field: PUSHED_AT, direction: DESC}) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        name stargazerCount forkCount pushedAt isArchived
        languages(first: 20, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""


def fetch_profile(login, token):
    """Profile fields plus every public non-fork repository."""
    repos = []
    cursor = None
    while True:
        data = gql(PROFILE_QUERY, {"login": login, "cursor": cursor}, token)
        user = data["user"]
        if user is None:
            raise SystemExit("no such user: " + login)
        block = user["repositories"]
        # The profile repo holds this generator, not actual work, so leaving it
        # in would make the card partly a report on itself.
        repos.extend(r for r in block["nodes"] if r["name"] != login)
        if not block["pageInfo"]["hasNextPage"]:
            break
        cursor = block["pageInfo"]["endCursor"]
    user["repositories"]["nodes"] = repos
    return user


def fetch_contributions(login, created_year, this_year, token):
    """One contributionsCollection per year; the API caps a span at 12 months."""
    parts = []
    for year in range(created_year, this_year + 1):
        parts.append(
            'y{y}: contributionsCollection('
            'from: "{y}-01-01T00:00:00Z", to: "{y}-12-31T23:59:59Z") {{ '
            'totalCommitContributions totalPullRequestContributions '
            'totalIssueContributions totalPullRequestReviewContributions '
            'totalRepositoriesWithContributedCommits }}'.format(y=year))
    query = ("query($login: String!) { user(login: $login) { " +
             " ".join(parts) + " } }")
    return gql(query, {"login": login}, token)["user"]


# --------------------------------------------------------------------------
# Avatar to coloured ASCII
# --------------------------------------------------------------------------

RAMP = " .,:;+=xX$&@"


def avatar_ascii(url, cols, rows):
    """Return rows of (char, colour) pairs, or None if it cannot be built."""
    try:
        from PIL import Image, ImageEnhance, ImageFilter, ImageOps
    except ImportError:
        print("Pillow not installed, using fallback art", file=sys.stderr)
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "profile-card"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
        img = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception as exc:                      # network or decode failure
        print("avatar unavailable (%s), using fallback art" % exc,
              file=sys.stderr)
        return None

    img = img.resize((cols, rows), Image.LANCZOS)
    img = ImageEnhance.Color(img).enhance(1.2)
    # equalize spreads a flat, mid-tone photo across the whole ramp; plain
    # autocontrast leaves a face as undifferentiated mush at this size. The
    # smoothing pass afterwards removes the single-cell speckle it introduces.
    grey = ImageOps.equalize(ImageOps.autocontrast(img.convert("L"), cutoff=2))
    grey = grey.filter(ImageFilter.SMOOTH)

    out = []
    for y in range(rows):
        line = []
        for x in range(cols):
            lum = grey.getpixel((x, y))
            char = RAMP[min(len(RAMP) - 1, lum * len(RAMP) // 256)]
            r, g, b = img.getpixel((x, y))
            # Quantise so neighbouring cells merge into a single text run.
            r, g, b = (r // 28) * 28, (g // 28) * 28, (b // 28) * 28
            line.append((char, "#%02x%02x%02x" % (r, g, b)))
        out.append(line)
    return out


FALLBACK_ART = [
    "                    ...,,,,,,,,...                  ",
    "                .,:;++======++;:,.                  ",
    "             ,:;+=xX$$&&&&&&$$Xx=+;:,               ",
    "          .,;+xX$&&@@@@@@@@@@@@&&$Xx+;,.            ",
    "         ,;=X$&@@@@@@@@@@@@@@@@@@@@&$X=;,           ",
    "       .;+X&@@@@@@@@@@@@@@@@@@@@@@@@@@&X+;.        ",
    "      ,;x$@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@$x;,       ",
    "     .;X&@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@&X;.      ",
    "     ;x&@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@&x;      ",
    "    .X@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@X.     ",
    "    ;&@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@&;     ",
    "    x@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@x     ",
    "    x@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@x     ",
    "    ;&@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@&;     ",
    "    .X@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@X.     ",
    "     ;x&@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@&x;      ",
    "     .;X&@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@&X;.      ",
    "      ,;x$@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@$x;,       ",
    "       .;+X&@@@@@@@@@@@@@@@@@@@@@@@@@@&X+;.        ",
    "         ,;=X$&@@@@@@@@@@@@@@@@@@@@&$X=;,          ",
    "          .,;+xX$&&@@@@@@@@@@@@&&$Xx+;,.           ",
    "             ,:;+=xX$$&&&&&&$$Xx=+;:,              ",
    "                .,:;++======++;:,.                 ",
]


# --------------------------------------------------------------------------
# Stats
# --------------------------------------------------------------------------

def human_bytes(n):
    value = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "%.0f B" % value
            return "%.1f %s" % (value, unit)
        value /= 1024


def uptime(created):
    born = dt.datetime.strptime(created, "%Y-%m-%dT%H:%M:%SZ")
    now = dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    months = (now.year - born.year) * 12 + (now.month - born.month)
    if now.day < born.day:
        months -= 1
    years, months = divmod(max(months, 0), 12)
    parts = []
    if years:
        parts.append("%dy" % years)
    parts.append("%dm" % months)
    return " ".join(parts)


def collect(user, contributions, this_year):
    repos = user["repositories"]["nodes"]
    langs = Counter()
    colors = {}
    for repo in repos:
        for edge in repo["languages"]["edges"]:
            name = edge["node"]["name"]
            langs[name] += edge["size"]
            if edge["node"]["color"]:
                colors[name] = edge["node"]["color"]

    commits = prs = issues = reviews = 0
    for key, block in contributions.items():
        if not key.startswith("y"):
            continue
        commits += block["totalCommitContributions"]
        prs += block["totalPullRequestContributions"]
        issues += block["totalIssueContributions"]
        reviews += block["totalPullRequestReviewContributions"]

    current = contributions.get("y%d" % this_year, {})
    top = max(repos, key=lambda r: r["stargazerCount"]) if repos else None
    return {
        "login": user["login"],
        "location": user["location"],
        "created": user["createdAt"],
        "followers": user["followers"]["totalCount"],
        "following": user["following"]["totalCount"],
        "repos": len(repos),
        "archived": sum(1 for r in repos if r["isArchived"]),
        "stars": sum(r["stargazerCount"] for r in repos),
        "forks": sum(r["forkCount"] for r in repos),
        "langs": langs,
        "lang_colors": colors,
        "commits": commits,
        "commits_this_year": current.get("totalCommitContributions", 0),
        "prs": prs,
        "issues": issues,
        "reviews": reviews,
        "contributed_to": current.get(
            "totalRepositoriesWithContributedCommits", 0),
        "top_repo": top,
        "last_push": max((r["pushedAt"] for r in repos), default=None),
    }


# --------------------------------------------------------------------------
# SVG
# --------------------------------------------------------------------------

def esc(text):
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def text_el(x, y, text, fill, weight=None):
    attrs = [
        'x="%.2f"' % x, 'y="%.2f"' % y, 'fill="%s"' % fill,
        'textLength="%.2f"' % (len(text) * FONT_W),
        'lengthAdjust="spacingAndGlyphs"', 'xml:space="preserve"',
    ]
    if weight:
        attrs.append('font-weight="%s"' % weight)
    return "<text %s>%s</text>" % (" ".join(attrs), esc(text))


def merge_runs(cells):
    """cells: (char, fill) pairs. Yields (start_col, text, fill) runs."""
    start, text, fill = 0, "", None
    for col, (char, cell_fill) in enumerate(cells):
        if cell_fill != fill and text:
            yield start, text, fill
            start, text = col, ""
        fill = cell_fill
        text += char
    if text:
        yield start, text, fill


class Terminal:
    """Appends absolutely positioned monospace runs on a character grid."""

    def __init__(self, theme):
        self.t = theme
        self.parts = []
        self.row = 0

    def row_top(self, row=None):
        return CHROME_H + PAD_TOP + (self.row if row is None else row) * LINE_H

    def put(self, col, text, fill, weight=None, row=None):
        if not text:
            return
        baseline = self.row_top(row) + FONT_SIZE * 0.78
        self.parts.append("  " + text_el(PAD_X + col * FONT_W, baseline,
                                         text, fill, weight))

    def spans(self, col, runs, row=None):
        """runs: (text, fill) pairs laid out left to right starting at col."""
        for text, fill in runs:
            self.put(col, text, fill, row=row)
            col += len(text)

    def raw(self, markup):
        self.parts.append("  " + markup)

    def prompt(self, col, command, row=None):
        self.spans(col, [("$ ", self.t["accent"]), (command, self.t["fg"])],
                   row=row)

    def scaled_grid(self, rows_cells, scale, row=None):
        """Draw a character grid at `scale`, giving it 1/scale the resolution."""
        inner = []
        for index, cells in enumerate(rows_cells):
            baseline = index * LINE_H + FONT_SIZE * 0.78
            for col, text, fill in merge_runs(cells):
                inner.append(text_el(col * FONT_W, baseline, text, fill))
        self.parts.append(
            '  <g transform="translate(%.2f,%.2f) scale(%s)">%s</g>'
            % (PAD_X, self.row_top(row), scale, "".join(inner)))
        return math.ceil(len(rows_cells) * scale)


def info_rows(stats):
    rows = [
        ("Uptime", uptime(stats["created"])),
        ("Repos", "%d public, %d archived" %
            (stats["repos"], stats["archived"])),
        ("Stars", "%d earned" % stats["stars"]),
        ("Forks", "%d of my repos" % stats["forks"]),
        ("Followers", "%d (following %d)" %
            (stats["followers"], stats["following"])),
        ("Commits", "%s lifetime, %s this year" %
            (format(stats["commits"], ","),
             format(stats["commits_this_year"], ","))),
        ("Pull Reqs", "%d opened, %d reviewed" %
            (stats["prs"], stats["reviews"])),
        ("Issues", "%d opened" % stats["issues"]),
        ("Active In", "%d repos this year" % stats["contributed_to"]),
        ("Languages", "%d tracked" % len(stats["langs"])),
        ("Source", "%s indexed" % human_bytes(sum(stats["langs"].values()))),
    ]
    if stats["top_repo"]:
        rows.append(("Top Repo", "%s (%d stars)" %
                     (stats["top_repo"]["name"],
                      stats["top_repo"]["stargazerCount"])))
    if stats["location"]:
        rows.append(("Location", stats["location"]))
    if stats["last_push"]:
        rows.append(("Last Push", stats["last_push"][:10]))
    return rows


def render(stats, avatar, theme_name, top_langs):
    t = THEMES[theme_name]
    term = Terminal(t)

    # --- left column: neofetch ---------------------------------------------
    term.prompt(0, "neofetch --ascii avatar.png")
    body_row = 2

    if avatar:
        cache = {}
        for cell_color in {c for row in avatar for _, c in row}:
            cache[cell_color] = fit_contrast(cell_color, t["avatar_lum"])
        themed = [[(char, cache[c]) for char, c in row] for row in avatar]
        art_rows = term.scaled_grid(themed, AVATAR_SCALE, row=body_row)
    else:
        art_rows = term.scaled_grid(
            [[(c, t["accent"]) for c in line] for line in FALLBACK_ART],
            AVATAR_SCALE, row=body_row)

    title = "%s@github" % stats["login"]
    term.put(INFO_COL, title, t["accent"], weight="bold", row=body_row)
    term.put(INFO_COL, "-" * len(title), t["border"], row=body_row + 1)

    rows = info_rows(stats)
    for index, (key, value) in enumerate(rows):
        term.spans(INFO_COL, [
            (key, t["key"]),
            (" " + "." * (INFO_KEY_W - len(key)) + " ", t["dim"]),
            (value, t["fg"]),
        ], row=body_row + 2 + index)
    left_rows = max(art_rows, 2 + len(rows))

    # --- right column: language bars ---------------------------------------
    term.prompt(LANG_COL, "langstats --by-bytes --top %d" % len(top_langs))

    total = sum(stats["langs"].values()) or 1
    for index, (name, size) in enumerate(top_langs):
        pct = size / total * 100.0
        filled = max(1, int(round(pct / 100.0 * BAR_CELLS)))
        color = stats["lang_colors"].get(
            name, FALLBACK_LANG_COLORS[index % len(FALLBACK_LANG_COLORS)])
        term.spans(LANG_COL, [
            (name[:12].ljust(13), t["fg"]),
            ("█" * filled, fit_contrast(color, t["lang_lum"])),
            ("░" * (BAR_CELLS - filled), t["track"]),
            ("  %5.1f%%" % pct, t["fg"]),
            ("%10s" % human_bytes(size), t["dim"]),
        ], row=body_row + index)

    right_rows = len(top_langs)
    shown = sum(size for _, size in top_langs)
    if shown < total:
        term.put(LANG_COL, "+ %d more language%s, %.1f%% combined" %
                 (len(stats["langs"]) - len(top_langs),
                  "" if len(stats["langs"]) - len(top_langs) == 1 else "s",
                  (total - shown) / total * 100.0),
                 t["dim"], row=body_row + right_rows + 1)
        right_rows += 2

    # --- blinking cursor ----------------------------------------------------
    term.row = body_row + max(left_rows, right_rows) + 1
    term.put(0, "$ ", t["accent"])
    term.raw('<rect x="%.2f" y="%.2f" width="%.2f" height="%.2f" fill="%s">'
             '<animate attributeName="opacity" values="1;1;0;0" dur="1.06s" '
             'repeatCount="indefinite"/></rect>'
             % (PAD_X + 2 * FONT_W, term.row_top(), FONT_W, FONT_SIZE,
                t["fg"]))

    # --- window chrome ------------------------------------------------------
    width = PAD_X * 2 + COLS * FONT_W
    height = CHROME_H + PAD_TOP + (term.row + 1) * LINE_H + PAD_BOTTOM
    window_title = "%s@github: ~" % stats["login"]

    head = [
        '<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
        'viewBox="0 0 %.0f %.0f" font-family="%s" font-size="%.1f" '
        'role="img" aria-label="%s GitHub statistics">'
        % (width, height, width, height, FONT_STACK, FONT_SIZE,
           esc(stats["login"])),
        '  <rect x="0.5" y="0.5" width="%.0f" height="%.0f" rx="10" '
        'fill="%s" stroke="%s"/>'
        % (width - 1, height - 1, t["bg"], t["border"]),
        '  <path d="M0.5 10.5a10 10 0 0 1 10-10 h%.0f a10 10 0 0 1 10 10 '
        'v%.0f H0.5 Z" fill="%s"/>'
        % (width - 21, CHROME_H - 10, t["chrome"]),
        '  <line x1="0.5" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s"/>'
        % (CHROME_H, width - 0.5, CHROME_H, t["border"]),
    ]
    for index, color in enumerate(t["dots"]):
        head.append('  <circle cx="%.0f" cy="%.0f" r="5.5" fill="%s"/>'
                    % (20 + index * 20, CHROME_H / 2, color))
    head.append('  <text x="%.0f" y="%.1f" fill="%s" text-anchor="middle" '
                'font-size="12">%s</text>'
                % (width / 2, CHROME_H / 2 + 4, t["title"], esc(window_title)))

    return "\n".join(head + term.parts + ["</svg>", ""])


# --------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="build profile stat cards")
    parser.add_argument("--user", default="kpg-anon")
    parser.add_argument("--out-dir",
                        default=os.path.dirname(os.path.abspath(__file__)))
    parser.add_argument("--top", type=int, default=12,
                        help="number of languages to chart")
    args = parser.parse_args()

    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        raise SystemExit("set GH_TOKEN or GITHUB_TOKEN "
                         "(gh auth token prints one)")

    user = fetch_profile(args.user, token)
    created_year = int(user["createdAt"][:4])
    this_year = dt.datetime.now(dt.timezone.utc).year
    contributions = fetch_contributions(args.user, created_year, this_year,
                                        token)
    stats = collect(user, contributions, this_year)

    avatar = avatar_ascii(user["avatarUrl"], AVATAR_COLS, AVATAR_ROWS)
    top_langs = stats["langs"].most_common(args.top)

    for theme in ("dark", "light"):
        svg = render(stats, avatar, theme, top_langs)
        path = os.path.join(args.out_dir, "%s_mode.svg" % theme)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(svg)
        print("wrote %s (%d bytes)" % (path, len(svg.encode("utf-8"))))


if __name__ == "__main__":
    main()
