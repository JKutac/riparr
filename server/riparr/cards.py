"""Name episodes from their title cards.

A DVD season doesn't record which episode each title is, so Riparr numbers them in disc
order -- a guess, and on a box set that carries an episode out of the guide's order it
is a wrong one. Many shows answer the question on screen: a title card with the
episode's name, and on some DVD releases a "First aired November 8, 1954" line under
it. This reads those cards and matches them against the guide.

What is read, in order of trust:

  1. An air date. Printed plain, so OCR reads it well, and a date belongs to one
     episode. Matched against every guide that has dates (TVmaze and TMDB can disagree:
     one dates an episode by its first broadcast, the other by a later one), within a
     few days, and -- when a digit was misread -- by month and day alone, but only when
     that is unique.
  2. The episode's name, found in the frame's text. Only a long, exact (after
     punctuation) hit counts: a script-lettered card reads as noise, and a short name
     like "Pilot" turns up in anything.

Nothing is decided from one frame. A title counts as named only when at least two
frames agree and nothing else comes close; otherwise it is left for a person, with a
thumbnail of the frame that looked most like a card so they can see for themselves.

Where to look is the expensive part. A sitcom puts its card in the first seconds; a
drama with a cold open puts the title over the first shot after the theme, minutes in.
So: where this series' card was found last time, then the first twenty seconds, and --
once per disc, only when nothing turned up -- a sparse scan of the first six minutes.
Whatever is found there is remembered for the next title and the next disc.

Needs ffmpeg and tesseract (with English). Without them this does nothing and says so;
the rip goes on exactly as before.
"""
import datetime
import difflib
import logging
import os
import re
import shutil
import subprocess
import tempfile
import time

from . import db

log = logging.getLogger("riparr.cards")

# A frame is scaled up before OCR: DVD text is small, and Tesseract reads text at
# roughly 30 px a line far better than at 15.
SCALE = 2
# Binarisations tried per frame: (keep pixels brighter than N, or darker). White
# lettering over a picture is the common card; dark lettering on a plain card the other.
VARIANTS = (("bright", 180, "6"), ("bright", 180, "3"), ("bright", 150, "6"),
            ("dark", 90, "11"))
# How far an OCR'd date may be from the guide's and still be that episode. None: a
# weekly show's neighbours are seven days apart, and a misread digit ("DEC. 8" read
# as "DEC. 3") lands within a few days of the wrong one. Found on a real season.
DATE_SLACK_DAYS = 0
# Frames that must agree before a title is named.
AGREE = 2
# The shortest guide name matched in a frame's text.
MIN_NAME = 6
FIRST_WINDOW = 20            # seconds, from the start
DEEP_WINDOW = (20, 360, 2)   # start, end, step: the once-per-disc wider look
CHUNK = 40                   # seconds of frames extracted at a time in the deep scan
TOOL_TIMEOUT = 60

_MONTHS = ["JANUARY", "FEBRUARY", "MARCH", "APRIL", "MAY", "JUNE", "JULY", "AUGUST",
           "SEPTEMBER", "OCTOBER", "NOVEMBER", "DECEMBER"]
# A month-ish word, a day, a year. The month is checked separately and loosely: OCR
# reads "OCT" as "OCL" or "QCT" far more often than it gets a digit wrong.
_DATE = re.compile(r"([A-Z][A-Z.:;]{1,9})[\s.,]*(\d{1,2})(?:ST|ND|RD|TH)?[\s.,]+"
                   r"((?:19|20)\d\d)\b")


def _month(word):
    """The month a misread word most likely is, 1-12, or None when it is not clearly
    one month."""
    w = re.sub(r"[^A-Z]", "", word.upper())
    if len(w) < 3:
        return None
    scores = []
    for i, m in enumerate(_MONTHS):
        target = m if len(w) > 4 else m[:len(w)] if len(w) >= 3 else m[:3]
        scores.append((difflib.SequenceMatcher(None, w, target).ratio(), i + 1))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    if best[0] >= 0.6 and best[0] - second[0] >= 0.15:
        return best[1]
    return None


# ── tools ────────────────────────────────────────────────────────────────────

def tools():
    """(ffmpeg, tesseract) paths, or None for any that is missing or unusable."""
    ff = shutil.which("ffmpeg")
    tess = shutil.which("tesseract")
    if tess:
        try:
            langs = subprocess.run([tess, "--list-langs"], capture_output=True,
                                   text=True, timeout=20)
            if "eng" not in (langs.stdout + langs.stderr).split():
                tess = None
        except Exception:
            tess = None
    return ff, tess


def available():
    ff, tess = tools()
    return bool(ff and tess)


def card_dir(job_id):
    return os.path.join(os.path.dirname(os.path.abspath(db.DB_PATH)), "cards",
                        "job-%d" % int(job_id))


def thumb_path(job_id, title_index):
    return os.path.join(card_dir(job_id), "t%d.jpg" % int(title_index))


# ── frames ───────────────────────────────────────────────────────────────────

def _extract(ff, path, start, length, step, out_dir):
    """Grey frames, scaled up, every `step` seconds from `start` for `length` seconds.
    Returns [(seconds, file)]. Empty if the file can't be read."""
    for f in os.listdir(out_dir):
        os.remove(os.path.join(out_dir, f))
    # Each pixel's darkest channel, not its brightness: white lettering is bright in all
    # three, a pink or sky-blue background in only one or two. In plain grey a pink
    # card is nearly as bright as the white date printed on it.
    graph = ("[0:v]fps=1/%s,scale=iw*%d:ih*%d,format=gbrp,extractplanes=g+b+r[g][b][r];"
             "[g][b]blend=all_mode=darken[gb];[gb][r]blend=all_mode=darken,format=gray"
             % (step, SCALE, SCALE))
    cmd = [ff, "-nostdin", "-loglevel", "error", "-ss", str(start), "-t", str(length),
           "-i", path, "-filter_complex", graph, "-f", "image2",
           os.path.join(out_dir, "f%04d.pgm")]
    try:
        subprocess.run(cmd, capture_output=True, timeout=TOOL_TIMEOUT + length)
    except Exception as e:
        log.info("Couldn't read frames from %s: %s", path, e)
        return []
    files = sorted(f for f in os.listdir(out_dir) if f.endswith(".pgm"))
    return [(start + i * step, os.path.join(out_dir, f)) for i, f in enumerate(files)]


def _read_pgm(path):
    """(header, pixels) of a binary greyscale PGM, as ffmpeg writes it."""
    with open(path, "rb") as fh:
        raw = fh.read()
    # P5\nW H\nMAX\n then the pixels. ffmpeg writes no comments.
    parts = raw.split(b"\n", 3)
    if len(parts) < 4 or parts[0] != b"P5":
        return None, None
    return b"\n".join(parts[:3]) + b"\n", parts[3]


def _binarise(pixels, kind, level):
    """Black text on white, the way Tesseract reads best."""
    if kind == "bright":
        table = bytes(0 if i > level else 255 for i in range(256))
    else:
        table = bytes(0 if i < level else 255 for i in range(256))
    return pixels.translate(table)


def _plausible_ink(ink):
    """Whether a binarised frame has about as much ink as text does: a few lines of
    lettering, not a blank and not a whole bright sky. Skipping the rest is what keeps
    Tesseract -- the slow part -- to the frames that might say something."""
    dark = ink.count(0)
    return 0.001 * len(ink) < dark < 0.35 * len(ink)


# Words that say a frame is a card even when the whole-frame read garbled the date.
_HINT = re.compile(r"AIR|FIRST|BROADCAST|\b(19|20)\d\d\b")


def _bands(tess, header, ink, work):
    """The frame read again a strip at a time, each as one line. Background texture
    around a line of text drags Tesseract's whole-page read off; alone, the line reads.
    Yields each strip's text."""
    try:
        w, h = (int(x) for x in header.split(b"\n")[1].split())
    except (ValueError, IndexError):
        return
    tall = h // 5
    for top in range(0, h - tall + 1, tall // 2):
        strip = ink[top * w:(top + tall) * w]
        if not _plausible_ink(strip):
            continue
        yield _ocr(tess, b"P5\n%d %d\n255\n" % (w, tall), strip, work, "7")


def _ocr(tess, header, pixels, work, psm="6"):
    p = os.path.join(work, "ocr.pgm")
    with open(p, "wb") as fh:
        fh.write(header + pixels)
    try:
        r = subprocess.run([tess, p, "-", "--psm", psm], capture_output=True, text=True,
                           timeout=TOOL_TIMEOUT,
                           env=dict(os.environ, OMP_THREAD_LIMIT="1"))
        return r.stdout or ""
    except Exception as e:
        log.info("tesseract failed: %s", e)
        return ""


def thumbnail(ff, path, at, out):
    """A small JPEG of the frame at `at` seconds, square pixels, for the hold page."""
    os.makedirs(os.path.dirname(out), exist_ok=True)
    cmd = [ff, "-nostdin", "-loglevel", "error", "-y", "-ss", str(max(0, at)), "-i", path,
           "-frames:v", "1", "-vf", "scale=trunc(ih*dar/2)*2:ih,scale=360:-2",
           "-q:v", "5", out]
    try:
        subprocess.run(cmd, capture_output=True, timeout=TOOL_TIMEOUT)
    except Exception as e:
        log.info("Couldn't make a thumbnail of %s: %s", path, e)
    return os.path.exists(out)


# ── matching ─────────────────────────────────────────────────────────────────

def dates_in(text):
    """Every date in a frame's text, as datetime.date."""
    out = []
    for m in _DATE.finditer(text.upper()):
        month = _month(m.group(1))
        if month is None:
            continue
        try:
            out.append(datetime.date(int(m.group(3)), month, int(m.group(2))))
        except ValueError:
            continue
    return out


def name_key(x):
    x = re.sub(r"^(the|a|an)\s+", "", (x or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", " ", x).strip()


class Guide:
    """What a frame is matched against: every dated, named, regular episode the guides
    list for this series. Built from several lists, because they date episodes
    differently and a card has only one date on it."""

    def __init__(self, *episode_lists):
        self.dated = []          # (date, name)
        self.names = {}          # name key -> name
        for listed in episode_lists:
            for e in listed or []:
                if e.get("special") or not e.get("name"):
                    continue
                key = name_key(e["name"])
                if key:
                    self.names.setdefault(key, e["name"])
                try:
                    d = datetime.date.fromisoformat(e.get("airdate") or "")
                except ValueError:
                    continue
                self.dated.append((d, e["name"]))

    def by_date(self, d):
        """(name, exact) of the one episode this date belongs to, or (None, False).
        `exact` False means it was found only by the misread-year fallback, which is
        weaker evidence and counts only when nothing exact was read for that title."""
        near = {name_key(n): n for when, n in self.dated
                if abs((when - d).days) <= DATE_SLACK_DAYS}
        if near:
            return (next(iter(near.values())), True) if len(near) == 1 else (None, False)
        # A misread digit in the year (1954 read as 1951): the month and day are
        # usually still right. Only when three of the year's four digits agree, and
        # exactly one episode has that month and day.
        near = {name_key(n): n for when, n in self.dated
                if (when.month, when.day) == (d.month, d.day)
                and sum(a == b for a, b in zip(str(when.year), str(d.year))) >= 3}
        return (next(iter(near.values())), False) if len(near) == 1 else (None, False)

    def by_name(self, text):
        """The one guide name that appears whole in this text, or None."""
        flat = " %s " % name_key(text)
        hits = [k for k in self.names if len(k) >= MIN_NAME and " %s " % k in flat]
        # A longer name containing a shorter one ("Ricky's Screen Test" and "Screen
        # Test") is the longer one.
        hits = [k for k in hits if not any(k != o and k in o for o in hits)]
        return self.names[hits[0]] if len(hits) == 1 else None


def readings(text, guide):
    """[(name, how, evidence)] this frame's text supports."""
    out = []
    for d in dates_in(text):
        name, exact = guide.by_date(d)
        if name:
            out.append((name, "date" if exact else "date~", d.isoformat()))
    if not out:
        name = guide.by_name(text)
        if name:
            out.append((name, "title", ""))
    return out


def verdict(votes):
    """The name enough frames agree on, or None. `votes` is {name: count}."""
    if not votes:
        return None
    ranked = sorted(votes.items(), key=lambda kv: -kv[1])
    top, n = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0
    return top if n >= AGREE and n > 2 * second else None


# ── one title ────────────────────────────────────────────────────────────────

class _Look:
    """What has been seen in one title so far."""

    def __init__(self):
        self.votes = {}
        self.weak = {}             # names found only through a misread year
        self.how = {}
        self.evidence = {}
        self.first_at = {}
        self.texty = (0, None)     # (letters seen, at) -- the frame most like a card

    def add(self, at, text, guide):
        letters = sum(c.isalpha() for c in text)
        if letters > self.texty[0]:
            self.texty = (letters, at)
        for name, how, ev in readings(text, guide):
            tally = self.weak if how == "date~" else self.votes
            tally[name] = tally.get(name, 0) + 1
            self.how.setdefault(name, how)
            if ev:
                self.evidence.setdefault(name, ev)
            self.first_at.setdefault(name, at)
            return True
        return False

    def named(self):
        return verdict(self.votes) if self.votes else verdict(self.weak)


def _scan(ff, tess, path, guide, look, start, end, step, work, deadline):
    """Read frames from `start` to `end`; stop as soon as the title is named."""
    t = start
    while t < end and time.time() < deadline:
        length = min(CHUNK, end - t)
        for at, f in _extract(ff, path, t, length, step, work):
            header, pixels = _read_pgm(f)
            if not header:
                continue
            for kind, level, psm in VARIANTS:
                ink = _binarise(pixels, kind, level)
                if not _plausible_ink(ink):
                    continue
                text = _ocr(tess, header, ink, work, psm)
                if look.add(at, text, guide):
                    break            # this frame said something; next frame
                if _HINT.search(text.upper()) and any(
                        look.add(at, band, guide)
                        for band in _bands(tess, header, ink, work)):
                    break
            if look.named():
                return True
            if time.time() > deadline:
                break
        t += length
    return bool(look.named())


def read_title(ff, tess, path, guide, offset=None, deep=False, deadline=None):
    """Look for one title's card. Returns a dict for the plan row:
    {"name", "how", "evidence", "at", "text_at"} -- name None when it couldn't tell."""
    deadline = deadline or time.time() + 600
    look = _Look()
    work = tempfile.mkdtemp(prefix="riparr-cards-")
    try:
        windows = []
        if offset is not None:
            windows.append((max(0, offset - 3), offset + 5, 1))
        windows.append((0, FIRST_WINDOW, 1))
        if deep:
            windows.append(DEEP_WINDOW)
        for start, end, step in windows:
            if _scan(ff, tess, path, guide, look, start, end, step, work, deadline):
                break
    finally:
        shutil.rmtree(work, ignore_errors=True)
    name = look.named()
    return {"name": name,
            "how": look.how.get(name) if name else None,
            "evidence": look.evidence.get(name, "") if name else "",
            "at": look.first_at.get(name) if name else None,
            "text_at": look.texty[1],
            "seen": sorted((look.votes or look.weak).items(), key=lambda kv: -kv[1])[:3]}


# ── a whole disc ─────────────────────────────────────────────────────────────

def offset_key(series_id):
    return "card_offset_%s" % series_id


# Remembered per series: where its card was found, in seconds, or NO_CARDS when a whole
# disc of it showed none -- so the next disc of a show without cards skips the long look.
NO_CARDS = -1
# Titles in a row that show nothing at all before a disc stops looking.
GIVE_UP_AFTER = 2


def read_disc(job_id, rows, guide, series_id=None, cancel=None, budget=1800):
    """Read every row's card. `rows` are plan rows with a readable `path`. Returns
    {title_index: result} (see `read_title`), each with "thumb" True when a thumbnail
    was saved. Does nothing when the tools are missing.

    The deep scan runs at most once per disc, on the first title that shows nothing in
    its opening seconds: a show whose card is minutes in tells us where on that first
    title, and a show with no card at all would only pay for the look again on every
    title. After GIVE_UP_AFTER titles that show no text worth matching, the rest only
    get thumbnails.
    """
    ff, tess = tools()
    if not (ff and tess):
        return {}
    deadline = time.time() + budget
    remembered = db.get(offset_key(series_id)) if series_id else None
    offset = remembered if isinstance(remembered, (int, float)) and remembered >= 0 else None
    deep = remembered is None
    blank, found_any, out = 0, False, {}
    for row in rows:
        if cancel is not None and cancel.is_set():
            break
        path = row.get("path")
        ti = int(row["title_index"])
        if not path or not os.path.exists(path):
            continue
        if blank >= GIVE_UP_AFTER and not found_any:
            res = {"name": None, "how": None, "evidence": "", "at": None,
                   "text_at": None, "seen": [], "skipped": True}
        else:
            res = read_title(ff, tess, path, guide, offset=offset, deep=deep,
                             deadline=deadline)
            deep = False
            if res["seen"]:
                found_any = True
            else:
                blank += 1
        if res["name"] and res["at"] is not None:
            if offset is None or abs(res["at"] - offset) > 5:
                offset = res["at"]
                if series_id:
                    db.set(offset_key(series_id), offset)
        at = res["at"] if res["at"] is not None else res["text_at"]
        if at is None:
            secs = int(row.get("seconds") or 0)
            at = min(150, secs // 5) if secs else 10
        res["thumb"] = thumbnail(ff, path, at, thumb_path(job_id, ti))
        res["thumb_at"] = at
        out[ti] = res
        log.info("Job %s title %d: %s", job_id, ti,
                 "%s (%s %s)" % (res["name"], res["how"], res["evidence"])
                 if res["name"] else "no card read %s" % res["seen"])
    if series_id and out and not found_any and remembered is None:
        db.set(offset_key(series_id), NO_CARDS)
    return out


def sweep(keep_days=30):
    """Remove thumbnails of jobs finished long ago."""
    root = os.path.dirname(card_dir(0))
    if not os.path.isdir(root):
        return
    cutoff = time.time() - keep_days * 86400
    for d in os.listdir(root):
        p = os.path.join(root, d)
        try:
            if os.path.getmtime(p) < cutoff:
                shutil.rmtree(p, ignore_errors=True)
        except OSError:
            pass
