#!/usr/bin/env python3
"""Bolt Cabinet Lookup - admin CLI (stdlib only, no deps).

Run on the container, from the Proxmox console or `pct enter`:

    python3 server/adminctl.py bootstrap        first super-user, once
    python3 server/adminctl.py add-user         add a coworker
    python3 server/adminctl.py list-users
    python3 server/adminctl.py set-perms <id>
    python3 server/adminctl.py deactivate <id>  reversible; prefer over delete
    python3 server/adminctl.py unlock <id>       clear a lockout, PIN unchanged
    python3 server/adminctl.py reset-pin <id>
    python3 server/adminctl.py backup
    python3 server/adminctl.py list-backups
    python3 server/adminctl.py restore <stamp>
    python3 server/adminctl.py export           fold overrides into bins.json
    python3 server/adminctl.py import-shelves <file> [--apply] [--counts]
                                                load a counted shelf list
    python3 server/adminctl.py set-shelf <record-id> <location> [level]
                                                one record, by id

PINs are always prompted for, never taken as an argument -- an argument
would land in the shell history of a shared box. Nothing here ever prints a
PIN, a hash, or the session key.
"""
import sys, os, io, re, json, getpass, subprocess, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server as S                      # single source of truth for hashing,
                                        # paths, permission catalog, backups

SERVICE = "bolt-cabinet"


def die(msg, code=1):
    print("error: " + msg, file=sys.stderr)
    sys.exit(code)


def ask(prompt, allow_empty=False):
    try:
        v = input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(130)
    if not v and not allow_empty:
        die("cancelled -- nothing entered")
    return v


def ask_pin(badge_id, prompt="PIN (not shown): "):
    """Prompt twice, no echo, and enforce the same rules the API enforces."""
    while True:
        try:
            a = getpass.getpass(prompt)
            b = getpass.getpass("Repeat it: ")
        except (EOFError, KeyboardInterrupt):
            print()
            sys.exit(130)
        if a != b:
            print("  those didn't match, try again")
            continue
        problem = S.pin_problem(a, badge_id)
        if problem:
            print("  " + problem)
            continue
        return a


def ask_perms(current=None):
    """Interactive flag picker. Prints the catalog straight from server.py so
    the two can never drift apart."""
    keys = sorted(S.PERMS)
    current = set(current or ["view"])
    print("\nPermissions:")
    for i, k in enumerate(keys, 1):
        mark = "x" if k in current else " "
        print(f"  {i}. [{mark}] {k:<15} {S.PERMS[k]}")
    print("Enter the numbers to grant, comma separated (blank keeps the marked set).")
    raw = ask("> ", allow_empty=True)
    if not raw:
        return sorted(current)
    out = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        if not part.isdigit() or not (1 <= int(part) <= len(keys)):
            die(f"'{part}' is not one of 1-{len(keys)}")
        out.add(keys[int(part) - 1])
    if not out:
        die("no permissions selected")
    return sorted(out)


def load_or_die():
    return S.load_users()


def get_user(doc, uid):
    u = S.find_user(doc, uid)
    if not u:
        die(f"no user with badge {uid}")
    return u


def cli_audit(action, target, **fields):
    S.audit({"id": "cli", "name": os.environ.get("SUDO_USER") or os.environ.get("USER") or "console"},
            action, target, via="adminctl", **fields)


# ------------------------------------------------------------------ users ---
def cmd_bootstrap(argv):
    doc = load_or_die()
    if doc.get("users"):
        die("users already exist -- bootstrap is for an empty roster only.\n"
            "       Use add-user, or reset-pin if you are locked out.")
    print("Creating the first super-user. This account gets every permission.\n")
    uid = ask("Badge number (digits): ")
    if not S.RE_BADGE.match(uid):
        die("badge number must be 1-12 digits")
    name = ask("Full name: ")
    pin = ask_pin(uid)
    rec = S.new_user_record(uid, name, pin, sorted(S.PERMS))
    doc.setdefault("users", []).append(rec)
    S.save_users(doc)
    cli_audit("bootstrap", uid, name=name, perms=sorted(S.PERMS))
    print(f"\nCreated {name} ({uid}) with: {', '.join(sorted(S.PERMS))}")
    print("Log in through the app and confirm before adding anyone else.")


def cmd_add_user(argv):
    doc = load_or_die()
    if not doc.get("users"):
        die("no users yet -- run `bootstrap` first")
    uid = argv[0] if argv else ask("Badge number (digits): ")
    if not S.RE_BADGE.match(uid):
        die("badge number must be 1-12 digits")
    if S.find_user(doc, uid):
        die(f"badge {uid} already exists -- use set-perms or reset-pin")
    name = ask("Full name: ")
    perms = ask_perms(["view"])
    pin = ask_pin(uid)
    doc["users"].append(S.new_user_record(uid, name, pin, perms))
    S.save_users(doc)
    cli_audit("user_create", uid, name=name, perms=perms)
    print(f"\nCreated {name} ({uid}) with: {', '.join(perms)}")


def cmd_list_users(argv):
    doc = load_or_die()
    users = doc.get("users", [])
    if not users:
        print("No users yet. Run `bootstrap`.")
        return
    print(f"{'BADGE':<10} {'NAME':<22} {'STATUS':<9} PERMISSIONS")
    for u in users:
        status = "active" if u.get("active", True) else "INACTIVE"
        if u.get("locked_until") and float(u["locked_until"]) > __import__("time").time():
            status = "LOCKED"
        print(f"{u.get('id',''):<10} {u.get('name',''):<22} {status:<9} "
              f"{', '.join(u.get('perms') or []) or '-'}")
    print(f"\n{len(users)} user(s). PIN material is never displayed.")


def cmd_set_perms(argv):
    if not argv:
        die("usage: set-perms <badge>")
    doc = load_or_die()
    u = get_user(doc, argv[0])
    before = list(u.get("perms") or [])
    perms = ask_perms(before)
    if "manage_users" not in perms and S.admins_left(doc, without_id=u["id"], as_perms=perms) == 0:
        die("that would leave nobody able to manage users")
    u["perms"] = perms
    S.save_users(doc)
    cli_audit("user_update", u["id"], before={"perms": before}, after={"perms": perms})
    print(f"{u.get('name')} ({u['id']}) now has: {', '.join(perms)}")


def cmd_deactivate(argv):
    if not argv:
        die("usage: deactivate <badge>")
    doc = load_or_die()
    u = get_user(doc, argv[0])
    if S.admins_left(doc, without_id=u["id"]) == 0:
        die("that would leave nobody able to manage users")
    S.make_backup(f"cli deactivate {u['id']}")
    u["active"] = False
    S.save_users(doc)
    cli_audit("user_deactivate", u["id"], name=u.get("name"))
    print(f"{u.get('name')} ({u['id']}) deactivated. Their audit history is kept.")


def cmd_unlock(argv):
    """Clear a badge lockout without changing anyone's PIN."""
    if not argv:
        die("usage: unlock <badge>")
    doc = load_or_die()
    u = get_user(doc, argv[0])
    before = {"failed": u.get("failed", 0), "locked_until": u.get("locked_until")}
    u["failed"] = 0
    u["locked_until"] = None
    S.save_users(doc)
    cli_audit("user_unlock", u["id"], before=before)
    print(f"{u.get('name')} ({u['id']}) unlocked. Their PIN is unchanged.")
    print("Note: IP lockouts live in memory -- `systemctl restart bolt-cabinet`")
    print("      clears those, or just log in once as a super-user.")


def cmd_reset_pin(argv):
    if not argv:
        die("usage: reset-pin <badge>")
    doc = load_or_die()
    u = get_user(doc, argv[0])
    print(f"Resetting the PIN for {u.get('name')} ({u['id']}).")
    pin = ask_pin(u["id"], "New PIN (not shown): ")
    S.make_backup(f"cli pin reset {u['id']}")
    u["pin"] = S.hash_pin(pin)
    u["failed"] = 0
    u["locked_until"] = None            # a reset also clears a lockout
    S.save_users(doc)
    cli_audit("user_pin_reset", u["id"])
    print("PIN reset. Any lockout on that badge is cleared.")


# ---------------------------------------------------------------- backups ---
def cmd_backup(argv):
    dest = S.make_backup("cli manual")
    print(f"Backed up to {dest}")
    print(f"Keeping the most recent {S.KEEP_BACKUPS}; older ones were pruned.")


def cmd_list_backups(argv):
    if not os.path.isdir(S.BACKUPS):
        print("No backups yet.")
        return
    for d in sorted(os.listdir(S.BACKUPS)):
        p = os.path.join(S.BACKUPS, d)
        if not os.path.isdir(p):
            continue
        reason = ""
        try:
            with open(os.path.join(p, "reason.json")) as f:
                reason = json.load(f).get("reason", "")
        except Exception:
            pass
        print(f"  {d}   {reason}")


def cmd_restore(argv):
    if not argv:
        die("usage: restore <timestamp>   (see list-backups)")
    src = os.path.join(S.BACKUPS, argv[0])
    if not os.path.isdir(src):
        die(f"no backup directory {src}")
    files = [f for f in ("users.json", "state.json", "overrides.json", "additions.json")
             if os.path.exists(os.path.join(src, f))]
    if not files:
        die("that backup contains none of the live data files")
    print(f"About to restore {', '.join(files)} from {argv[0]}.")
    print("The current files will be backed up first, then the service restarted.")
    if ask("Type RESTORE to continue: ") != "RESTORE":
        die("cancelled")
    S.make_backup(f"pre-restore of {argv[0]}")
    _service("stop")
    import shutil
    for f in files:
        shutil.copy2(os.path.join(src, f), os.path.join(S.APPDIR, f))
    os.chmod(os.path.join(S.APPDIR, "users.json"), 0o600)
    _service("start")
    cli_audit("restore", argv[0], files=files)
    print(f"Restored {len(files)} file(s) and restarted {SERVICE}.")


def _service(action):
    try:
        subprocess.run(["systemctl", action, SERVICE], check=True)
        print(f"  systemctl {action} {SERVICE}")
    except Exception as e:
        print(f"  warning: could not {action} {SERVICE} ({e}).")
        print(f"  Run `systemctl {action} {SERVICE}` yourself.")


# ----------------------------------------------------------------- export ---
def cmd_export(argv):
    """Fold overrides back into the git-tracked seed and bump the version.

    Writes public/bins.json and nothing else -- no commit, no push, and the
    overrides file is left alone. Review the diff on the dev machine and
    commit deliberately; clear overrides.json only once that commit has been
    pulled onto the container.
    """
    with_additions = "--with-additions" in argv
    data = S.load_bins()
    ov = S.load_overrides().get("overrides", {})
    adds = S.load_additions()

    changed = 0
    out = []
    for r in data["bins"]:
        rec = dict(r)
        o = ov.get(rec.get("id"))
        if o:
            touched = False
            for k in S.OVERRIDABLE:
                if k in o and rec.get(k) != o[k]:
                    rec[k] = o[k]
                    touched = True
            if touched:
                changed += 1
        out.append(rec)

    added = 0
    if with_additions:
        for r in adds:
            rec = dict(r)
            o = ov.get(rec.get("id"))
            if o:
                for k in S.OVERRIDABLE:
                    if k in o:
                        rec[k] = o[k]
            for k in ("boxes", "binFull"):
                rec.pop(k, None)        # live counts belong in state.json
            out.append(rec)
            added += 1

    stamp = datetime.date.today().isoformat()
    prev = str(data.get("version", ""))
    version = stamp + "a"
    if prev.startswith(stamp):          # already exported today: a -> b -> c
        suffix = prev[len(stamp):] or "a"
        version = stamp + chr(ord(suffix[0]) + 1)

    S.write_json(S.BINS, {"version": version, "count": len(out), "bins": out})
    print(f"Wrote {S.BINS}")
    print(f"  version {prev} -> {version}")
    print(f"  {changed} record(s) updated from overrides.json"
          + (f", {added} addition(s) folded in" if with_additions else ""))
    if adds and not with_additions:
        print(f"  note: {len(adds)} record(s) in additions.json were NOT included."
              "\n        Re-run with --with-additions to fold those in too.")
    print("\nNothing was committed. Review the diff, then commit deliberately.")
    print("Clear overrides.json only after that commit is live on the container.")

def cmd_set_shelf(argv):
    """Set one record's back-stock location, addressed by record id.

    import-shelves resolves by last 4, which is exactly what is ambiguous
    when two records share one. This takes the id instead, so a collision
    can be settled deliberately -- including giving the same shelf to a part
    that lives in a bin in each cabinet.

      set-shelf B-Y1 "Rack 2" Top --boxes 0 --note "shared with CA-T4"
      set-shelf B-O8 "Rack 1" Top --boxes 7
      set-shelf A-E1 none                      clears it
    """
    args, opts = [], {}
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("--boxes", "--note"):
            if i + 1 >= len(argv):
                die(f"{a} needs a value")
            opts[a] = argv[i + 1]; i += 2
        else:
            args.append(a); i += 1
    if len(args) < 2:
        die('usage: set-shelf <record-id> <location|none> [level] '
            '[--boxes N] [--note "..."]\n'
            '       locations: ' + ", ".join(S.BACKSTOCK_RACKS))

    rid, where = args[0], args[1]
    level = args[2] if len(args) > 2 else ""

    live = {r["id"]: r for r in S.merged()["bins"]}
    rec = live.get(rid)
    if not rec:
        die(f"no record with id {rid}")

    if where.lower() in ("none", "clear", "-"):
        patch, err = S.clean_backstock("", "")
    else:
        # Accept a location name case-insensitively; the level must still be
        # one that location actually has, which clean_backstock enforces.
        match = [k for k in S.BACKSTOCK_RACKS if k.lower() == where.lower()]
        if not match:
            die("unknown location %r -- try one of: %s"
                % (where, ", ".join(S.BACKSTOCK_RACKS)))
        patch, err = S.clean_backstock(match[0], level)
    if err:
        die(err)

    print("%s  %s  (%s)" % (rid, rec["pn"],
                            "C%s-%s" % (rec["cab"], rec["bin"])
                            if rec["cab"] != "?" else "no bin"))
    print("   shelf  %s -> %s" % (
        (rec.get("bsRack") or "none") + (" " + rec["bsLevel"] if rec.get("bsLevel") else ""),
        (patch["bsRack"] or "none") + (" " + patch["bsLevel"] if patch["bsLevel"] else "")))

    with S._lock:
        doc = S.load_overrides()
        entry = doc.setdefault("overrides", {}).setdefault(rid, {})
        before = {k: entry.get(k) for k in ("bsRack", "bsLevel")}
        entry.update(patch)
        if "--note" in opts:
            entry["note"] = S.clean_note(opts["--note"])
            print("   note   %s" % entry["note"])
        entry["edited_by"] = "cli"
        entry["edited_at"] = S.now_iso()
        S.save_overrides(doc)

        state = S.load_state()
        if "--boxes" in opts:
            try:
                n = max(0, int(opts["--boxes"]))
            except ValueError:
                die("--boxes needs a whole number")
            st = state.setdefault(rid, {"boxes": 0, "binFull": False})
            print("   boxes  %d -> %d" % (st.get("boxes", 0), n))
            st["boxes"] = n
        S.stamp(state, rid, {"id": "cli", "name": "shelf count"},
                "back stock %s" % ((patch["bsRack"] +
                                    (" \u00b7 " + patch["bsLevel"] if patch["bsLevel"] else ""))
                                   if patch["bsRack"] else "location cleared"))
        S.save_state(state)
        cli_audit("set_shelf", rid, before=before, after=patch,
                  boxes=opts.get("--boxes"))
    print("Done. Picked up live; no restart needed.")


# ---------------------------------------------------------- shelf import ---
# A counted walk of the back-stock racks, in the form someone actually writes
# it on a clipboard: a location heading, then one line per BOX, by last 4.
# Repeats are boxes, not duplicates.
#
#     Rack 1
#     Top Shelf
#     6368
#     6368            <- two boxes of ...6368 on Rack 1 Top
#
# Headings match BACKSTOCK_RACKS in server.py. A location with no levels
# (Cabinet A Floor) takes its codes directly under the location heading.
def parse_shelf_list(path):
    """-> (entries, problems). entries are (location, level, last4)."""
    # "Top Shelf" / "top shelf" / "Second" all name the same level.
    levels = {}
    for loc, lv in S.BACKSTOCK_RACKS.items():
        for name in lv:
            levels.setdefault(loc, {})[name.lower()] = name
            levels[loc][name.lower() + " shelf"] = name
    locs = {k.lower(): k for k in S.BACKSTOCK_RACKS}

    entries, problems, loc, level = [], [], None, None
    for n, raw in enumerate(io.open(path, encoding="utf-8"), 1):
        line = raw.strip().lstrip("*-\u2022 ").strip()
        if not line:
            continue
        low = line.lower()
        if low in locs:
            loc = locs[low]
            level = None
            if not S.BACKSTOCK_RACKS[loc]:
                level = ""            # a spot: codes follow immediately
            continue
        if loc and low in levels.get(loc, {}):
            level = levels[loc][low]
            continue
        if re.fullmatch(r"\d{4}", line):
            if loc is None or level is None:
                problems.append("line %d: %s before any location/shelf heading" % (n, line))
            else:
                entries.append((loc, level, line))
            continue
        problems.append("line %d: not a heading or a 4-digit code: %r" % (n, line))
    return entries, problems


def cmd_import_shelves(argv):
    """Resolve a counted shelf list onto records and write the result.

    Dry run unless --apply. Last-4 codes collide by design in this data, so
    anything that does not land on exactly one record is reported and
    skipped rather than guessed at. A part found on more than one shelf
    keeps the shelf holding the most boxes and gets the full spread in its
    note -- a record holds one location, and silently dropping the rest
    would send someone to the wrong shelf.
    """
    args = [a for a in argv if not a.startswith("--")]
    if not args:
        die("usage: import-shelves <file> [--apply] [--counts]")
    path, apply_it, do_counts = args[0], "--apply" in argv, "--counts" in argv
    if not os.path.exists(path):
        die(f"no such file: {path}")

    entries, problems = parse_shelf_list(path)
    if problems:
        print("Could not read %d line(s):" % len(problems))
        for p in problems:
            print("   " + p)
        if apply_it:
            die("fix the file first -- nothing was written.")

    counts = {}                                   # (loc, level, last4) -> boxes
    for e in entries:
        counts[e] = counts.get(e, 0) + 1
    listed = {}                                   # last4 -> total boxes
    spread = {}                                   # last4 -> {(loc, level): boxes}
    for (loc, level, c), n in counts.items():
        listed[c] = listed.get(c, 0) + n
        spread.setdefault(c, {})[(loc, level)] = n

    by4 = {}
    for r in S.merged()["bins"]:
        by4.setdefault(str(r["pn"])[-4:], []).append(r)

    plan, skipped = [], []
    for c in sorted(spread):
        match = by4.get(c, [])
        if len(match) != 1:
            skipped.append((c, listed[c],
                            "no record ends in %s" % c if not match else
                            "%d records end in %s: %s" % (
                                len(match), c, ", ".join(r["id"] for r in match))))
            continue
        rec = match[0]
        where = sorted(spread[c].items(), key=lambda kv: (-kv[1], kv[0]))
        (loc, level), top = where[0]
        note = ""
        if len(where) > 1:
            note = "also " + ", ".join(
                "%s%s x%d" % (l, " " + lv if lv else "", n) for (l, lv), n in where[1:])
        plan.append({"id": rec["id"], "pn": rec["pn"], "last4": c,
                     "rack": loc, "level": level, "note": note,
                     "boxes_now": rec.get("boxes") or 0, "boxes_listed": listed[c]})

    print("\n%d box entries, %d distinct codes" % (len(entries), len(spread)))
    print("%d code(s) resolve to exactly one record" % len(plan))
    print("%d code(s) skipped\n" % len(skipped))

    print("%-6s %-9s %-11s %-18s %-8s %s" % ("LAST4", "RECORD", "PART", "LOCATION", "BOXES", "NOTE"))
    for p in plan:
        boxes = ("%d" % p["boxes_listed"] if p["boxes_now"] == p["boxes_listed"]
                 else "%d->%d" % (p["boxes_now"], p["boxes_listed"]))
        print("%-6s %-9s %-11s %-18s %-8s %s" % (
            p["last4"], p["id"], p["pn"][:11],
            p["rack"] + (" " + p["level"] if p["level"] else ""),
            boxes if do_counts else str(p["boxes_listed"]), p["note"]))

    if skipped:
        print("\nSkipped -- resolve these by hand:")
        for c, n, why in skipped:
            print("   %s  (%d box(es))  %s" % (c, n, why))

    changed_counts = [p for p in plan if p["boxes_now"] != p["boxes_listed"]]
    if do_counts:
        print("\n%d record(s) would have their box count changed." % len(changed_counts))
    elif changed_counts:
        print("\n%d record(s) have a different count in the list. Re-run with"
              " --counts to apply those too." % len(changed_counts))

    if not apply_it:
        print("\nDRY RUN -- nothing written. Re-run with --apply.")
        return

    S.make_backup("import-shelves")
    with S._lock:
        doc = S.load_overrides()
        ovs = doc.setdefault("overrides", {})
        state = S.load_state()
        for p in plan:
            bs, err = S.clean_backstock(p["rack"], p["level"])
            if err:                      # headings came from BACKSTOCK_RACKS
                die("unexpected: %s" % err)
            entry = ovs.setdefault(p["id"], {})
            before = {k: entry.get(k) for k in ("bsRack", "bsLevel")}
            entry.update(bs)
            if p["note"]:
                entry["note"] = p["note"]
            entry["edited_by"] = "cli"
            entry["edited_at"] = S.now_iso()
            if do_counts:
                st = state.setdefault(p["id"], {"boxes": 0, "binFull": False})
                st["boxes"] = p["boxes_listed"]
            S.stamp(state, p["id"], {"id": "cli", "name": "shelf count"},
                    "back stock %s%s" % (p["rack"],
                                         " \u00b7 " + p["level"] if p["level"] else ""))
            cli_audit("import_shelf", p["id"], before=before, after=bs,
                      note=p["note"] or None,
                      boxes=p["boxes_listed"] if do_counts else None)
        S.save_overrides(doc)
        S.save_state(state)

    print("\nWrote %d record(s)%s." % (len(plan), " including box counts" if do_counts else ""))
    print("Front-end changes are picked up live; no restart needed.")
    print("Run `export` when you want these folded into the tracked seed.")


COMMANDS = {
    "bootstrap": cmd_bootstrap, "add-user": cmd_add_user, "list-users": cmd_list_users,
    "set-perms": cmd_set_perms, "deactivate": cmd_deactivate, "reset-pin": cmd_reset_pin,
    "unlock": cmd_unlock,
    "backup": cmd_backup, "list-backups": cmd_list_backups, "restore": cmd_restore,
    "export": cmd_export, "import-shelves": cmd_import_shelves,
    "set-shelf": cmd_set_shelf,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    cmd = COMMANDS.get(sys.argv[1])
    if not cmd:
        die(f"unknown command '{sys.argv[1]}'. Try --help.")
    cmd(sys.argv[2:])
