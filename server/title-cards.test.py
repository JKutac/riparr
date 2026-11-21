#!/usr/bin/env python3
"""Reading episodes' title cards (riparr/cards.py).

The strings below are what Tesseract really returned from DVD title cards while this
was built -- misread months and all -- so the parsing is tested against the mistakes
it actually has to survive. The guide traps (a misread digit landing on the next
week's episode; a misread day reaching another season through the year fallback)
were each found on a real season and each produced a wrong or tied answer before the
rule that now stops it.

The last section makes a real video with a card in it and reads it end to end. It
needs ffmpeg, tesseract (English) and a TrueType font, and is skipped without them.

Run: python3 server/title-cards.test.py
"""
import datetime
import glob
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_tmp = tempfile.mkdtemp(prefix="riparr-cards-test-")
os.environ["RIPARR_DB"] = os.path.join(_tmp, "test.db")

from riparr import cards, db  # noqa: E402

db.init()
failures = []


def check(name, got, want):
    if got == want:
        print("  ok   %s" % name)
    else:
        print("  FAIL %s: got %r, wanted %r" % (name, got, want))
        failures.append(name)


D = datetime.date

print("dates are read through the usual OCR mistakes")
for text, want in [
        ("FIRST AIRED QC::T, 4, 1954 ON CBS", [D(1954, 10, 4)]),
        ("FIRST AIRED OCL 4, 1954 ON CBS", [D(1954, 10, 4)]),
        ("FIRST AIRED OCE. 4,1954", [D(1954, 10, 4)]),
        ("FIRST AIRED NOVEMBER 15. 1954 ON", [D(1954, 11, 15)]),
        ("FIRST AIRED NOVEMBIEER 1, 1951", [D(1951, 11, 1)]),
        ("FIRST AIRED DEC. 13, 1954 ON CBS", [D(1954, 12, 13)]),
        ("FIRST AIRED JAN. 3, 1955 ON CBS", [D(1955, 1, 3)]),
        ("AIRED JUNE 2, 1952", [D(1952, 6, 2)]),
        ("FIRST AIRED 0?4, 1954 ON CBS", []),           # no month left to read
        ("SEASON 4 1954", []),                          # a word that is no month
        ("FIRST AIRED FEB. 30, 1954", []),              # no such day
]:
    check(repr(text), cards.dates_in(text), want)

print("a date names an episode only when it is that episode's")
guide = cards.Guide(
    [{"season": 1, "number": n, "name": "Weekly %d" % n,
      "airdate": (D(1952, 3, 3) + datetime.timedelta(weeks=n - 1)).isoformat()}
     for n in range(1, 9)]
    + [{"season": 6, "number": 1, "name": "Much Later", "airdate": "1957-04-01"},
       {"season": 1, "number": 99, "name": "A Special", "airdate": "1952-03-05",
        "special": True}],
    # A second guide dating one episode differently, the way TVmaze and TMDB can.
    [{"season": 1, "number": 5, "name": "Weekly 5", "airdate": "1952-03-30"}])
check("exact", guide.by_date(D(1952, 3, 17)), ("Weekly 3", True))
check("either guide's date counts", guide.by_date(D(1952, 3, 30)), ("Weekly 5", True))
check("a misread day near another week's episode is nothing",
      guide.by_date(D(1952, 3, 19)), (None, False))
check("a misread year keeps month and day: a weak answer",
      guide.by_date(D(1951, 3, 17)), ("Weekly 3", False))
check("but not when the year is too far off to be one misread digit",
      guide.by_date(D(1905, 3, 17)), (None, False))
check("specials don't take part", guide.by_date(D(1952, 3, 5)), (None, False))

print("a name on screen counts only whole and unique")
names = cards.Guide([{"season": 1, "number": 1, "name": "The Long Goodbye"},
                     {"season": 1, "number": 2, "name": "Goodbye"},
                     {"season": 1, "number": 3, "name": "Pilot"},
                     {"season": 1, "number": 4, "name": "Station Break"}])
check("whole name", names.by_name("STAR SHOW | Station Break"), "Station Break")
check("the longer name, not the one inside it",
      names.by_name("the long goodbye"), "The Long Goodbye")
check("too short to trust", names.by_name("PILOT"), None)
check("part of a word is not a name", names.by_name("Stationbreaker"), None)

print("frames vote, and weak readings count only when nothing exact was read")
check("two agree", cards.verdict({"A": 2}), "A")
check("one frame is not enough", cards.verdict({"A": 1}), None)
check("a close second is a tie", cards.verdict({"A": 4, "B": 2}), None)
check("a clear winner", cards.verdict({"A": 5, "B": 2}), "A")
look = cards._Look()
for text in ["FIRST AIRED MARCH 17, 1952", "FIRST AIRED MARCH 17, 1952",
             "FIRST AIRED MARCH 24, 1951", "FIRST AIRED MARCH 24, 1951",
             "FIRST AIRED MARCH 24, 1951"]:
    look.add(0, text, guide)
check("exact readings outvote misread-year ones", look.named(), "Weekly 3")
look = cards._Look()
for text in ["FIRST AIRED MARCH 24, 1951", "FIRST AIRED MARCH 24, 1951"]:
    look.add(0, text, guide)
check("misread-year readings alone still name it", look.named(), "Weekly 4")

print("frames worth reading: about as much ink as a few lines of text")
check("a line of text is ink", cards._plausible_ink(bytes(50) + bytes([255]) * 9950), True)
check("a blank isn't", cards._plausible_ink(bytes([255]) * 10000), False)
check("nor a frame that is mostly ink", cards._plausible_ink(bytes(9000) + bytes([255]) * 1000),
      False)

print("a card in a real video is found and read")
ff, tess = cards.tools()
font = next(iter(glob.glob("/usr/share/fonts/**/DejaVuSerif-Bold.ttf", recursive=True)
                 + glob.glob("/usr/share/fonts/**/DejaVuSans-Bold.ttf", recursive=True)),
            None)
if not (ff and tess and font):
    print("  skip ffmpeg, tesseract or a font is missing")
else:
    video = os.path.join(_tmp, "episode.mkv")
    words = os.path.join(_tmp, "card.txt")
    with open(words, "w") as fh:       # a file: a comma in text= ends the filter
        fh.write("FIRST AIRED MARCH 24, 1952 ON TV")
    draw = ("drawtext=fontfile=%s:textfile=%s:fontsize=26:fontcolor=white:"
            "x=(w-tw)/2:y=h*0.82:enable='between(t,5,11)'" % (font, words))
    subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x8a2a3a:s=720x480:d=24:r=10", "-vf", draw, "-c:v",
                    "libx264", "-preset", "ultrafast", video], check=True)
    res = cards.read_disc(1, [{"title_index": 0, "path": video, "seconds": 24}],
                          guide, series_id=5)
    got = res.get(0) or {}
    check("named from the date", (got.get("name"), got.get("how")), ("Weekly 4", "date"))
    check("found where the card is", 5 <= (got.get("at") or 0) <= 11, True)
    check("a thumbnail kept", os.path.isfile(cards.thumb_path(1, 0)), True)
    check("and where it was remembered for the series",
          5 <= (db.get(cards.offset_key(5)) or 0) <= 11, True)
    blank = os.path.join(_tmp, "blank.mkv")
    subprocess.run([ff, "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=0x203040:s=720x480:d=24:r=10", "-c:v", "libx264",
                    "-preset", "ultrafast", blank], check=True)
    rows = [{"title_index": i, "path": blank, "seconds": 24} for i in range(4)]
    res = cards.read_disc(2, rows, guide, series_id=6, budget=300)
    check("a disc without cards names nothing", [r["name"] for r in res.values()],
          [None] * 4)
    check("stops looking after two blank titles",
          [bool(r.get("skipped")) for r in res.values()], [False, False, True, True])
    check("but still keeps a frame of every title",
          all(r["thumb"] for r in res.values()), True)
    check("and remembers the show has none", db.get(cards.offset_key(6)),
          cards.NO_CARDS)

print("")
shutil.rmtree(_tmp, ignore_errors=True)
if failures:
    print("%d FAILED: %s" % (len(failures), ", ".join(failures)))
    sys.exit(1)
print("all good")
