#!/usr/bin/env python3
"""
watchaudit - know before you delete.

A read-only audit that joins per-account watch history from Plex and/or
Jellyfin (read directly from their own databases) to the folders and bytes
on your disk, and classifies every show and movie into decision tiers:

  IN_PROGRESS   a household account is part-way through it (hands off)
  NEVER_ANYONE  nobody on either server has ever watched any of it
  SHARED_ONLY   only non-household Plex shares watched it, household never
  STALE         household watched, but nothing in --stale-days days
  ACTIVE        household watched within --stale-days days
  EMPTY         folder is (nearly) empty, e.g. an *arr placeholder

It never writes to the databases, never calls a server API, and never
deletes anything. It prints a report; the deleting is your decision.

Why direct database reads? Because the Plex API, when used with the server
owner's token, cannot scope watch state to individual Plex Home profiles -
it returns the server-wide aggregate. Tools built on the API therefore
cannot tell you WHO watched something in a multi-profile household, and
sync tools have been observed smearing the aggregate onto every profile.
The databases are the ground truth each server itself recorded.

Tested against: Plex Media Server 1.43.x database, Jellyfin 12.x database.
Other versions may work; the tool checks for the tables it needs and
fails loudly if they are missing.
"""

import argparse
import csv
import os
import re
import sqlite3
import sys
from datetime import date, timedelta

PLEX_TABLES = {"metadata_items", "metadata_item_views",
               "metadata_item_settings", "accounts", "media_items",
               "media_parts", "library_sections"}
JF_TABLES = {"BaseItems", "UserData", "Users"}


def fail(msg):
    print(f"watchaudit: error: {msg}", file=sys.stderr)
    sys.exit(2)


def open_ro(path, needed, label):
    if not os.path.isfile(path):
        fail(f"{label} database not found: {path}")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    have = {r[0] for r in con.execute(
        "select name from sqlite_master where type='table'")}
    missing = needed - have
    if missing:
        con.close()
        fail(f"{label} database {path} is missing tables {sorted(missing)}. "
             "This version's schema is not supported (see README for tested "
             "versions). No data was modified - the database is opened "
             "read-only.")
    return con


def folder_of_file(path, folder_names):
    """Return the media folder a path belongs to: the deepest path
    component that is one of the known on-disk folder names. Works for
    file paths (the filename component never matches a folder name) and
    for Jellyfin series paths, which point at the folder itself."""
    parts = [p for p in re.split(r"[/\\]", path or "") if p]
    hit = None
    for p in parts:
        if p in folder_names:
            hit = p
    return hit


def scan_root(root):
    """{folder_name: size_bytes} for each direct child folder of root."""
    out = {}
    if not root:
        return out
    if not os.path.isdir(root):
        fail(f"media root not found: {root}")
    for name in sorted(os.listdir(root)):
        full = os.path.join(root, name)
        if not os.path.isdir(full):
            continue
        total = 0
        for dirpath, _dirs, files in os.walk(full):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(dirpath, f))
                except OSError:
                    pass
        out[name] = total
    return out


def split_title_year(folder):
    m = re.match(r"^(.*) \((\d{4})\)$", folder)
    return (m.group(1), m.group(2)) if m else (folder, "")


class Title:
    def __init__(self, folder, kind, size):
        self.folder, self.kind, self.size = folder, kind, size
        self.title, self.year = split_title_year(folder)
        self.plex_views = 0
        self.plex_views_house = 0
        self.plex_last_house = 0
        self.watched_any = False
        self.watched_house = False
        self.jf_plays = 0
        self.jf_watched = False
        self.jf_last = ""
        self.progress = []          # "Plex:Name" / "Jellyfin:Name"

    @property
    def gb(self):
        return self.size / 1073741824.0

    @property
    def house_watched(self):
        return self.watched_house or self.jf_watched or self.jf_plays > 0 \
            or self.plex_views_house > 0

    @property
    def any_watched(self):
        return self.house_watched or self.watched_any \
            or self.plex_views > 0

    @property
    def last_house(self):
        d = ""
        if self.plex_last_house:
            d = date.fromtimestamp(self.plex_last_house).isoformat()
        return max(d, self.jf_last)

    def tier(self, stale_cutoff):
        if self.size < 1048576:
            return "EMPTY"
        if self.progress:
            return "IN_PROGRESS"
        if not self.any_watched:
            return "NEVER_ANYONE"
        if not self.house_watched:
            return "SHARED_ONLY"
        if self.last_house and self.last_house <= stale_cutoff:
            return "STALE"
        return "ACTIVE"


class Census:
    """All titles, keyed by (kind, folder) - a show folder and a movie
    folder may share a name without colliding."""

    def __init__(self, tv_folders, movie_folders):
        self.tv_folders = tv_folders
        self.movie_folders = movie_folders
        self.titles = {}

    def get(self, kind, folder):
        if folder is None:
            return None
        key = (kind, folder)
        if key not in self.titles:
            sizes = self.tv_folders if kind == "show" else self.movie_folders
            self.titles[key] = Title(folder, kind, sizes.get(folder, 0))
        return self.titles[key]

    def by_title(self, kind, title):
        return [t for (k, _f), t in self.titles.items()
                if k == kind and t.title == title]

    def add_disk_folders(self):
        for folder in self.tv_folders:
            self.get("show", folder)
        for folder in self.movie_folders:
            self.get("movie", folder)


def load_plex(path, census, household_names):
    con = open_ro(path, PLEX_TABLES, "Plex")
    accounts = {r[0]: (r[1] or "") for r in con.execute(
        "select id, name from accounts")}
    household = {aid for aid, name in accounts.items()
                 if household_names is None or name in household_names}
    sections = {r[0]: r[1] for r in con.execute(
        "select id, section_type from library_sections")}
    movie_secs = {s for s, t in sections.items() if t == 1}
    tv_secs = {s for s, t in sections.items() if t == 2}

    # file paths -> owning title, movie/TV sections only (music and other
    # libraries can contain folders whose names collide with show folders)
    item_folder = {}   # metadata_item_id -> (kind, folder)
    for mid, sec, fpath in con.execute(
            "select mi.id, mi.library_section_id, mp.file "
            "from metadata_items mi "
            "join media_items mdi on mdi.metadata_item_id = mi.id "
            "join media_parts mp on mp.media_item_id = mdi.id"):
        if sec in tv_secs:
            kind, names = "show", census.tv_folders
        elif sec in movie_secs:
            kind, names = "movie", census.movie_folders
        else:
            continue
        folder = folder_of_file(fpath, set(names))
        if folder:
            item_folder[mid] = (kind, folder)
            census.get(kind, folder)
    parent = {r[0]: r[1] for r in con.execute(
        "select id, parent_id from metadata_items")}

    def title_of_item(mid):
        seen = set()
        cur = mid
        while cur and cur not in seen:
            seen.add(cur)
            if cur in item_folder:
                kind, folder = item_folder[cur]
                return census.get(kind, folder)
            cur = parent.get(cur)
        return None

    # views (play history with timestamps); episode plays roll up to the
    # show via grandparent_title, movie plays use the item title
    for gtitle, title_, mtype, acct, viewed in con.execute(
            "select grandparent_title, title, metadata_type, account_id, "
            "viewed_at from metadata_item_views"):
        if mtype == 4:
            kind, name = "show", gtitle
        elif mtype == 1:
            kind, name = "movie", title_
        else:
            continue
        if not name:
            continue
        for t in census.by_title(kind, name):
            t.plex_views += 1
            if acct in household:
                t.plex_views_house += 1
                if viewed and viewed > t.plex_last_house:
                    t.plex_last_house = viewed
    # per-account watched flags + resume points, by guid
    guid_item = {r[1]: r[0] for r in con.execute(
        "select id, guid from metadata_items")}
    for guid, acct, vcount, voffset in con.execute(
            "select guid, account_id, view_count, view_offset "
            "from metadata_item_settings"):
        mid = guid_item.get(guid)
        if mid is None:
            continue
        t = title_of_item(mid)
        if t is None:
            continue
        if vcount and vcount > 0:
            t.watched_any = True
            if acct in household:
                t.watched_house = True
        if voffset and voffset > 0 and acct in household:
            who = f"Plex:{accounts.get(acct, acct)}"
            if who not in t.progress:
                t.progress.append(who)
    con.close()


def load_jellyfin(path, census, household_names):
    con = open_ro(path, JF_TABLES, "Jellyfin")
    users = {r[0]: r[1] for r in con.execute("select Id, Username from Users")}
    household = {uid for uid, name in users.items()
                 if household_names is None or name in household_names}
    # leaf items only: Episode for shows, Movie for movies. Series/Season
    # items carry rolled-up copies of the same UserData and would
    # double-count; other item types (audio, live TV, ...) are ignored.
    leaf_folder = {}   # item id -> (kind, folder)
    for iid, path_, type_, series in con.execute(
            "select Id, Path, Type, SeriesName from BaseItems"):
        tname = type_ or ""
        if tname.endswith(".Episode"):
            kind, names = "show", census.tv_folders
        elif tname.endswith(".Movie"):
            kind, names = "movie", census.movie_folders
        else:
            continue
        folder = folder_of_file(path_, set(names)) if path_ else None
        if folder is None and series:
            for cand in names:
                if split_title_year(cand)[0] == series:
                    folder = cand
                    break
        if folder:
            leaf_folder[iid] = (kind, folder)
            census.get(kind, folder)
    for uid, iid, played, count, ticks, last in con.execute(
            "select UserId, ItemId, Played, PlayCount, "
            "PlaybackPositionTicks, LastPlayedDate from UserData"):
        if iid not in leaf_folder or uid not in household:
            continue
        kind, folder = leaf_folder[iid]
        t = census.get(kind, folder)
        if count:
            t.jf_plays += count
        if played:
            t.jf_watched = True
        if ticks and ticks > 0:
            who = f"Jellyfin:{users.get(uid, uid)}"
            if who not in t.progress:
                t.progress.append(who)
        if last and str(last)[:10] > t.jf_last:
            t.jf_last = str(last)[:10]
    con.close()


def main():
    ap = argparse.ArgumentParser(
        prog="watchaudit",
        description="Read-only cross-server (Plex + Jellyfin) watch "
                    "history audit joined to disk usage. Never deletes "
                    "anything.")
    ap.add_argument("--plex-db", help="path to com.plexapp.plugins."
                                      "library.db")
    ap.add_argument("--jellyfin-db", help="path to jellyfin.db")
    ap.add_argument("--tv-root", action="append", default=[],
                    help="folder containing show folders (repeatable)")
    ap.add_argument("--movie-root", action="append", default=[],
                    help="folder containing movie folders (repeatable)")
    ap.add_argument("--household-plex", default=None,
                    help="comma-separated Plex account names that count "
                         "as household; everyone else is a share. "
                         "Default: all accounts are household.")
    ap.add_argument("--household-jellyfin", default=None,
                    help="comma-separated Jellyfin usernames that count "
                         "as household. Default: all users.")
    ap.add_argument("--stale-days", type=int, default=365,
                    help="household inactivity that counts as STALE "
                         "(default 365)")
    ap.add_argument("--csv", help="also write the full table to this CSV")
    ap.add_argument("--top", type=int, default=25,
                    help="titles printed per tier (default 25)")
    args = ap.parse_args()

    if not args.plex_db and not args.jellyfin_db:
        fail("give at least one of --plex-db / --jellyfin-db")
    if not args.tv_root and not args.movie_root:
        fail("give at least one of --tv-root / --movie-root")

    tv_folders, movie_folders = {}, {}
    for r in args.tv_root:
        tv_folders.update(scan_root(r))
    for r in args.movie_root:
        movie_folders.update(scan_root(r))
    census = Census(tv_folders, movie_folders)

    hh_plex = None if args.household_plex is None else {
        s.strip() for s in args.household_plex.split(",") if s.strip()}
    hh_jf = None if args.household_jellyfin is None else {
        s.strip() for s in args.household_jellyfin.split(",") if s.strip()}

    if args.plex_db:
        load_plex(args.plex_db, census, hh_plex)
    if args.jellyfin_db:
        load_jellyfin(args.jellyfin_db, census, hh_jf)
    census.add_disk_folders()

    stale_cutoff = (date.today() - timedelta(days=args.stale_days)
                    ).isoformat()
    rows = sorted(census.titles.values(), key=lambda t: -t.size)
    for t in rows:
        t.tier_name = t.tier(stale_cutoff)

    total_gb = sum(t.gb for t in rows)
    print(f"watchaudit - {len(rows)} titles, {total_gb:,.0f} GB on disk")
    print(f"stale cutoff: {stale_cutoff} "
          f"({args.stale_days} days without household activity)")
    order = ["NEVER_ANYONE", "SHARED_ONLY", "STALE", "IN_PROGRESS",
             "ACTIVE", "EMPTY"]
    blurbs = {
        "NEVER_ANYONE": "never watched by anyone on either server",
        "SHARED_ONLY": "watched only by non-household shares",
        "STALE": "household watched, but not within the stale window",
        "IN_PROGRESS": "a household account is part-way through - hands off",
        "ACTIVE": "household watched within the stale window",
        "EMPTY": "empty folders (e.g. *arr placeholders for unreleased "
                 "titles)",
    }
    for tier in order:
        grp = [t for t in rows if t.tier_name == tier]
        if not grp:
            continue
        print(f"\n## {tier} - {len(grp)} titles, "
              f"{sum(t.gb for t in grp):,.0f} GB - {blurbs[tier]}")
        for t in grp[:args.top]:
            extra = ""
            if t.progress:
                extra = f"  [{', '.join(t.progress)}]"
            elif t.last_house:
                extra = f"  [last household activity {t.last_house}]"
            print(f"  {t.gb:8.1f} GB  {t.folder}{extra}")
        if len(grp) > args.top:
            print(f"  ... and {len(grp) - args.top} more (see CSV)")

    if args.csv:
        with open(args.csv, "w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["folder", "kind", "year", "size_gb", "tier",
                        "plex_views", "plex_views_household", "jf_plays",
                        "last_household_activity", "in_progress_by"])
            for t in rows:
                w.writerow([t.folder, t.kind, t.year, f"{t.gb:.1f}",
                            t.tier_name, t.plex_views, t.plex_views_house,
                            t.jf_plays, t.last_house,
                            ";".join(t.progress)])
        print(f"\nfull table written to {args.csv}")


if __name__ == "__main__":
    main()
