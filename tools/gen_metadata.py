#!/usr/bin/env python3
"""Regenerate every bundled dictionary in ``src/a4i/metadata`` from the MIM Reference.

The Cisco APIC Management Information Model Reference serves one JSON file per
class, and everything a4i knows about ACI statically is distilled from those
files here. Four artifacts come out:

``classes.txt``
    Concrete class names, one per line, for shell completion and ``a4i list class``.

``rn_formats.txt``
    ``class<TAB>rnFormat`` for every configurable class, for turning a body's
    child MO into a DN (:mod:`a4i.mo`).

``model.jsonl`` / ``model.idx``
    One distilled record per class, and the byte offsets to seek to them, for
    ``describe``.

``search.txt``
    ``class<TAB>label<TAB>summary``, for ``search``.

Run it as::

    python tools/gen_metadata.py                  # fetch, then build
    python tools/gen_metadata.py --skip-fetch     # rebuild from the cache alone

The fetch walks about 15,000 files, so it caches every response -- compressed,
as the server sent it -- and a rebuild after a change to the distillation below
costs nothing.

There is no class index on the server -- no directory listing, and every index
path 404s -- so the class list is discovered rather than read. The crawl starts
at ``topRoot`` and at whatever ``classes.txt`` already holds, and follows every
class reference each file carries (containment, inheritance and relations) until
it stops finding new ones. Seeding with the existing list means a regeneration
can only ever add classes, never silently drop the ones a4i already ships.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

BASE = "https://pubhub.devnetcloud.com/media/model-doc-latest/docs/doc/jsonmeta"

REPO = Path(__file__).resolve().parent.parent
OUT_DIR = REPO / "src" / "a4i" / "metadata"
CACHE_DIR = REPO / ".metadata-cache"

# Where the crawl starts when no dictionary exists yet. topRoot is the root of
# the whole management information tree, so following containment from it
# reaches every class that can exist on a fabric.
ROOT_CLASS = "topRoot"

# Two properties of the source blow the corpus up from a few megabytes to a few
# hundred, and neither carries information worth that.
#
# A class that can hang anywhere -- tagAnnotation is a child of nearly every MO
# in the model -- lists every DN it could ever have, tens of thousands of them,
# and 5 MB of DN templates answers no question a shorter list would not. A DN
# template is only worth showing when it names a few definite places.
MAX_DN_FORMATS = 3
# Fault codes and statistics thresholds enumerate thousands of valid values. The
# first two dozen show the shape; a count stands in for the rest.
MAX_VALID_VALUES = 24
# The same disease on the other side of containment: faultCounts, healthInst and
# tagAnnotation hang under nearly every class in the model, so listing their
# parents is 58 KB that says "anywhere". A few examples and a count says it in a
# line.
MAX_PARENTS = 8

# A summary line for search: the first sentence of the class comment, held short
# enough that the whole file stays scannable.
MAX_SUMMARY = 160

_PKG = re.compile(r"^([a-z0-9]+)([A-Z].*)$")


# -- fetching --------------------------------------------------------------


def url_for(class_name: str) -> str | None:
    """Return the URL of ``class_name``'s JSON, or None if it is not a class name.

    The package is the leading lowercase run of the name, so ``fvnsEncapBlk``
    lives at ``fvns/EncapBlk.json``.
    """

    match = _PKG.match(class_name)
    if match is None:
        return None
    return f"{BASE}/{match[1]}/{match[2]}.json"


def normalize(ref: str) -> str:
    """Turn a reference the source writes as ``fv:BD`` into the name a4i uses."""

    return ref.replace(":", "", 1)


def fetch_one(class_name: str, cache: Path, attempts: int = 4) -> bytes | None:
    """Return ``class_name``'s JSON, from the cache when it is already there.

    The server compresses these files about fifteen to one -- fvBD is 189 KB of
    JSON and 12 KB on the wire -- so the crawl asks for gzip and keeps what it
    got, compressed, in the cache. That is the difference between a cache of a
    few hundred megabytes and one of ten gigabytes, and between a crawl bound by
    bandwidth and one bound by round trips.

    A 404 is cached as an empty file: the model refers to classes whose
    documentation is not published, and re-asking for them on every run would
    cost a request each time to learn the same nothing.

    A crawl is tens of thousands of requests over some tens of minutes, which is
    long enough that a transient failure is expected rather than exceptional. One
    is retried rather than allowed to end the run, since the run is what holds
    the frontier.
    """

    path = cache / f"{class_name}.json.gz"
    if path.exists():
        raw = path.read_bytes()
        return gzip.decompress(raw) if raw else None
    url = url_for(class_name)
    if url is None:
        return None
    for attempt in range(attempts):
        # Compression is an optimisation, so it is the first thing given up on.
        # At least one file on the server -- eqptcapacityEstPGLabelUsage5min --
        # answers 502 to every gzip request and 200 to every plain one, so an
        # attempt that keeps asking for gzip never gets there however many times
        # it tries.
        headers = {} if attempt else {"Accept-Encoding": "gzip"}
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read()
                compressed = response.headers.get("Content-Encoding") == "gzip"
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                path.write_bytes(b"")
                return None
            if attempt == attempts - 1:
                print(f"warning: {class_name}: HTTP {exc.code}", file=sys.stderr)
                return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            if attempt == attempts - 1:
                print(f"warning: {class_name}: {exc}", file=sys.stderr)
                return None
        else:
            # The cache holds one shape whatever the server sent, so a plain
            # response is compressed here.
            path.write_bytes(body if compressed else gzip.compress(body))
            return gzip.decompress(body) if compressed else body
        time.sleep(min(2**attempt, 8))
    return None


def references(meta: dict[str, Any]) -> set[str]:
    """Return every class this one names, in any capacity.

    Containment alone would reach the whole tree, but inheritance and relations
    are followed too: an abstract superclass is contained by nothing, and a
    relation names classes the crawl would otherwise arrive at only by chance.
    """

    found: set[str] = set()
    for key in ("contains", "containedBy", "subClasses", "relationTo", "relationFrom"):
        value = meta.get(key)
        if isinstance(value, dict):
            found.update(value)
            found.update(v for v in value.values() if isinstance(v, str) and v)
    for ref in meta.get("superClasses") or []:
        found.add(ref)
    return {normalize(ref) for ref in found if ref}


def crawl(seeds: Iterable[str], cache: Path, jobs: int) -> dict[str, dict[str, Any]]:
    """Fetch every class reachable from ``seeds`` and return them by class name."""

    cache.mkdir(parents=True, exist_ok=True)
    found: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    frontier = {name for name in seeds if name}

    with ThreadPoolExecutor(max_workers=jobs) as pool:
        while frontier:
            batch = sorted(frontier - seen)
            seen.update(batch)
            frontier = set()
            fetched = pool.map(lambda name: fetch_one(name, cache), batch)
            for class_name, raw in zip(batch, fetched, strict=True):
                if not raw:
                    continue
                try:
                    document = json.loads(raw)
                except ValueError:
                    print(f"warning: {class_name}: unparseable JSON", file=sys.stderr)
                    continue
                if not document:
                    continue
                meta = document[next(iter(document))]
                found[class_name] = meta
                frontier.update(references(meta) - seen)
            print(
                f"  fetched {len(seen):,} classes, {len(frontier):,} newly referenced",
                file=sys.stderr,
            )
    return found


# -- distilling ------------------------------------------------------------


def summary(comment: Any) -> str:
    """Return a one-line summary of a class or property comment.

    The source writes a comment as a list of paragraphs. Only the first sentence
    of the first one is kept for the search index; ``describe`` shows the whole.
    """

    text = " ".join(comment) if isinstance(comment, list) else str(comment or "")
    text = " ".join(text.split())
    sentence = text.split(". ")[0].rstrip(".")
    if len(sentence) > MAX_SUMMARY:
        sentence = sentence[: MAX_SUMMARY - 1].rstrip() + "…"
    return sentence


def description(comment: Any) -> str:
    """Return a class or property comment as one string."""

    text = " ".join(comment) if isinstance(comment, list) else str(comment or "")
    return " ".join(text.split())


def distill_property(prop: dict[str, Any]) -> dict[str, Any]:
    """Return what an LLM needs of one property, and nothing else.

    A property it cannot set is reduced to what it means and what type it is:
    that is enough to read a GET response, which is the only place a read-only
    property is ever met. A settable one carries everything needed to write a
    valid value without a round trip -- the enum, the default, the regex and the
    bounds -- because getting that wrong is a POST the APIC rejects.
    """

    entry: dict[str, Any] = {
        "desc": description(prop.get("comment")),
        "type": prop.get("baseType") or "",
    }
    if not prop.get("isConfigurable"):
        entry["readOnly"] = True
        return entry

    for key, source in (
        ("naming", "isNaming"),
        ("mandatory", "mandatory"),
        ("createOnly", "createOnly"),
    ):
        if prop.get(source):
            entry[key] = True
    values = [
        value["localName"]
        for value in prop.get("validValues") or []
        # "defaultValue" is not a value the property takes: the source uses that
        # slot to point at which of the others is the default, which "default"
        # below already says.
        if value.get("localName") and value["localName"] != "defaultValue"
    ]
    if values:
        if len(values) > MAX_VALID_VALUES:
            entry["values"] = values[:MAX_VALID_VALUES]
            entry["moreValues"] = len(values) - MAX_VALID_VALUES
        else:
            entry["values"] = values
    if prop.get("default") is not None:
        entry["default"] = prop["default"]
    if prop.get("validators"):
        entry["validators"] = prop["validators"]
    return entry


def distill(class_name: str, meta: dict[str, Any], configurable: set[str]) -> dict[str, Any]:
    """Return the record ``describe`` serves for one class.

    A configurable class carries what it takes to build a POST body for it: how
    its RN is formed, which properties name it, what may hang under it. A class
    that can only be read carries what it takes to make sense of a GET response
    and no more -- its children are runtime objects nobody writes, and listing
    them would be the larger half of the corpus spent on a question nobody asks.
    """

    properties = {
        name: prop
        for name, prop in sorted((meta.get("properties") or {}).items())
        if not prop.get("isDeprecated") and not prop.get("isHidden")
    }
    configurable_class = bool(meta.get("isConfigurable"))
    parents = sorted(normalize(ref) for ref in meta.get("containedBy") or {})
    record: dict[str, Any] = {
        "class": class_name,
        "label": meta.get("label") or "",
        "desc": description(meta.get("comment")),
        "configurable": configurable_class,
        "parents": parents[:MAX_PARENTS],
    }
    if len(parents) > MAX_PARENTS:
        record["moreParents"] = len(parents) - MAX_PARENTS

    if not configurable_class:
        # Every property of a class nobody can configure is read-only, so saying
        # so per property is 14,000 classes' worth of the same word. What is left
        # is the name and the type, which is what reading a GET response takes.
        # Spelling out each one's meaning as well would quadruple the whole
        # dictionary for the classes least often asked about.
        record["props"] = {name: prop.get("baseType") or "" for name, prop in properties.items()}
        return record

    record["props"] = {name: distill_property(prop) for name, prop in properties.items()}

    record["rn"] = meta.get("rnFormat") or ""
    record["naming"] = list(meta.get("identifiedBy") or [])
    dn_formats = meta.get("dnFormats") or []
    if dn_formats and len(dn_formats) <= MAX_DN_FORMATS:
        record["dn"] = list(dn_formats)
    # Only the children that can be configured: an MO the fabric maintains for
    # itself is never something a body puts there, and the unfiltered list is
    # ten times longer.
    record["children"] = sorted(
        name
        for name in (normalize(ref) for ref in meta.get("contains") or {})
        if name in configurable and name != class_name
    )
    return record


# -- writing ---------------------------------------------------------------


_RELATION = re.compile(r"^[a-z0-9]+R[st][A-Z]")


def rank(class_name: str, meta: dict[str, Any]) -> int:
    """Return how likely this class is to be the one somebody searching means.

    Dozens of classes share a label. "Bridge Domain" is ``fvBD``, but it is also
    every relation pointing at one -- ``fhsRtBDToFhs``, ``dhcpRtBDToRelayP`` --
    and the abstract policy ``fvBD`` inherits from. Ordering the matches by what
    they are is what puts the object itself above the wiring around it.
    """

    if meta.get("isAbstract"):
        return 3
    if not meta.get("isConfigurable"):
        return 2
    # fvRsCtx points from here, fvRtBd points at here: plumbing between MOs
    # rather than a thing anybody sets out to configure.
    return 1 if _RELATION.match(class_name) else 0


def write_lines(path: Path, lines: Iterable[str]) -> int:
    text = "".join(f"{line}\n" for line in lines)
    path.write_text(text, encoding="utf-8")
    return len(text.encode())


def build(found: dict[str, dict[str, Any]], out: Path) -> None:
    """Write every dictionary from the crawled metadata.

    Raises :class:`SystemExit` rather than shipping a dictionary smaller than the
    one already there. A single 502 in a crawl of eighteen thousand requests is
    an ordinary event, and its cost -- a class silently gone from the shipped
    dictionary, and from shell completion with it -- is not something to discover
    from a bug report. Rerunning uses the cache, so the retry is cheap.
    """

    out.mkdir(parents=True, exist_ok=True)

    concrete = sorted(
        name
        for name, meta in found.items()
        if not meta.get("isAbstract") and not meta.get("isDeprecated")
    )
    lost = seeds_from(out) - set(concrete)
    if lost:
        raise SystemExit(
            f"error: {len(lost)} class(es) already shipped are missing from this crawl: "
            f"{', '.join(sorted(lost)[:10])}"
            f"{', ...' if len(lost) > 10 else ''}\n"
            "Nothing was written. Rerun to retry them (the cache keeps what already "
            "downloaded), or delete them from classes.txt if they are gone for good."
        )
    size = write_lines(out / "classes.txt", concrete)
    print(f"classes.txt     {len(concrete):>6,} classes  {size:>10,} B")

    rn_formats = sorted(
        (name, meta["rnFormat"])
        for name, meta in found.items()
        if meta.get("isConfigurable") and meta.get("rnFormat")
    )
    size = write_lines(out / "rn_formats.txt", (f"{n}\t{f}" for n, f in rn_formats))
    print(f"rn_formats.txt  {len(rn_formats):>6,} classes  {size:>10,} B")

    configurable = {name for name, meta in found.items() if meta.get("isConfigurable")}

    # The records are written in class-name order and the index is written
    # alongside them, so the two files stay parallel and a lookup is one seek.
    records = [(name, distill(name, found[name], configurable)) for name in sorted(found)]
    model = out / "model.jsonl"
    index: list[str] = []
    offset = 0
    with model.open("w", encoding="utf-8") as handle:
        for name, record in records:
            line = json.dumps(record, separators=(",", ":"), ensure_ascii=False) + "\n"
            encoded = len(line.encode())
            index.append(f"{name}\t{offset}\t{encoded}")
            handle.write(line)
            offset += encoded
    print(f"model.jsonl     {len(records):>6,} classes  {offset:>10,} B")
    size = write_lines(out / "model.idx", index)
    print(f"model.idx       {len(index):>6,} classes  {size:>10,} B")

    size = write_lines(
        out / "search.txt",
        (
            f"{name}\t{rank(name, found[name])}\t{record['label']}"
            f"\t{summary(found[name].get('comment'))}"
            for name, record in records
        ),
    )
    print(f"search.txt      {len(records):>6,} classes  {size:>10,} B")


# -- entry point -----------------------------------------------------------


def seeds_from(out: Path) -> set[str]:
    """Return the class names a4i already ships, so a rebuild cannot lose one."""

    path = out / "classes.txt"
    if not path.exists():
        return set()
    return set(path.read_text(encoding="utf-8").split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT_DIR, help="where to write the dictionaries")
    parser.add_argument("--cache", type=Path, default=CACHE_DIR, help="where to cache responses")
    parser.add_argument("--jobs", type=int, default=8, help="parallel downloads (default: 8)")
    parser.add_argument(
        "--skip-fetch",
        action="store_true",
        help="build from the cache alone, without asking the server for anything",
    )
    args = parser.parse_args(argv)

    if args.skip_fetch:
        found = {}
        for path in sorted(args.cache.glob("*.json.gz")):
            raw = path.read_bytes()
            if not raw:
                continue
            document = json.loads(gzip.decompress(raw))
            if document:
                found[path.name.removesuffix(".json.gz")] = document[next(iter(document))]
        print(f"loaded {len(found):,} classes from {args.cache}", file=sys.stderr)
    else:
        print(f"crawling from {BASE}", file=sys.stderr)
        found = crawl({ROOT_CLASS, *seeds_from(args.out)}, args.cache, args.jobs)
        print(f"crawled {len(found):,} classes", file=sys.stderr)

    if not found:
        print("error: no class metadata to build from", file=sys.stderr)
        return 1
    build(found, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
