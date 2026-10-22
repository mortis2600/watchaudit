# watchaudit

**Know before you delete.**

A read-only audit that joins per-account watch history from Plex and/or
Jellyfin — read directly from their own databases — to the folders and
bytes on your disk, and classifies every show and movie into decision
tiers:

| Tier | Meaning |
| --- | --- |
| `IN_PROGRESS` | A household account is part-way through it. Hands off. |
| `NEVER_ANYONE` | Nobody on either server has ever watched any of it. |
| `SHARED_ONLY` | Only non-household Plex shares watched it; the household never did. |
| `STALE` | Household watched, but nothing within `--stale-days` days. |
| `ACTIVE` | Household watched within the stale window. |
| `EMPTY` | Folder is (nearly) empty, e.g. an *arr placeholder for an unreleased title. |

It never writes to the databases, never calls a server API, and never
deletes anything. It prints a report; the deleting is your decision.

## Why not use the APIs like everything else?

Because for multi-profile households, the APIs can't tell the truth:

* **Plex's API, authenticated with the server owner's token, cannot scope
  watch state to individual Plex Home profiles.** It returns the
  server-wide aggregate. Any tool built on it — including well-known
  watched-state sync tools — sees one blended "everyone watched
  everything" blob, and sync tools have been observed writing that blend
  back onto every profile, smearing a family's separate watch histories
  into identical copies of each other.
* **Cleanup tools are single-server.** If your household watches on Plex
  *and* Jellyfin (very common mid-migration), a Plex-only tool will
  happily flag a show your family finished last week — on Jellyfin.

The databases are the ground truth each server itself recorded: every
play, watched flag, and resume point, with the account ID attached.
watchaudit reads them (SQLite read-only mode), unions both servers,
keeps every person's history separate, separates household accounts from
outside shares, and joins the result to your actual folders and gigabytes.

## Install

No dependencies. Python 3.9+ standard library only.

```sh
git clone https://github.com/mortis2600/watchaudit
cd watchaudit
```

## Usage

You need:

* The Plex database: `.../Plex Media Server/Plug-in Support/Databases/com.plexapp.plugins.library.db`
* and/or the Jellyfin database: `jellyfin.db` from Jellyfin's data directory
* The folders that contain your show folders and movie folders

```sh
python3 watchaudit.py \
  --plex-db "/path/to/com.plexapp.plugins.library.db" \
  --jellyfin-db "/path/to/jellyfin.db" \
  --tv-root /media/tv \
  --movie-root /media/movies \
  --household-plex "OwnerName,Partner,Kids" \
  --household-jellyfin "owner,partner,kids" \
  --csv watchaudit.csv
```

* `--household-plex` / `--household-jellyfin`: comma-separated account
  names that count as *your household*. Everyone else on Plex counts as a
  share (that's what makes `SHARED_ONLY` possible). Omit them and every
  account counts as household — the safe default.
* `--stale-days N`: household inactivity that counts as `STALE`
  (default 365).
* `--csv PATH`: write the full per-title table.
* Either database can be omitted for a single-server audit.

> **Back up your databases first** — not because watchaudit writes (it
> opens them read-only and physically cannot), but because poking around
> live server databases is a good habit to be careful in. On a live
> server, a SQLite `.backup` copy is the cleanest thing to point it at.

## Example output

```
watchaudit - 573 titles, 16,732 GB on disk
stale cutoff: 2025-10-22 (365 days without household activity)

## NEVER_ANYONE - 108 titles, 2,032 GB - never watched by anyone on either server
      97.8 GB  Example Show (2023)
      ...

## IN_PROGRESS - 24 titles, 1,301 GB - a household account is part-way through - hands off
     421.2 GB  Another Show  [Plex:Christina, Jellyfin:Christina]
      ...
```

## Tested versions

* Plex Media Server 1.43.x (database schema as of late 2026)
* Jellyfin 12.x

The tool checks for the tables it needs and fails loudly if a future
schema change removes them. Reports of other working versions welcome.

## Caveats

* Items are matched to folders by folder name (the servers record file
  paths; the audit walks them up to the folder under your TV/movie
  roots). A show folder and a movie folder that share a name are kept
  separate. Two folders with the same name in the *same* root are not
  supported.
* Play history (`metadata_item_views`) only goes back as far as your
  Plex server retains it; watched *flags* are cumulative. A title can be
  `STALE` by last-play date while still fully watched — read the tier
  table with the CSV's columns, not by vibes.
* Jellyfin users not listed in `--household-jellyfin` are ignored
  (Jellyfin has no "shares" concept in practice).

## Credits

Built out of a real household's migration from Plex to Jellyfin, after a
sync tool blended seven people's watch histories together and a cleanup
tool flagged shows the family was actively watching — on the other app.
Standard library only; no third-party code.

## License

MIT — see [LICENSE](LICENSE).

<!-- DONATE-BUTTON-START -->
## Support

If watchaudit saved you from deleting the wrong terabyte:

[![Donate with PayPal](https://www.paypalobjects.com/en_US/i/btn/btn_donate_LG.gif)](https://www.paypal.com/donate/?hosted_button_id=LS8RG7GS6GWGU)
<!-- DONATE-BUTTON-END -->
