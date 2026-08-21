#!/usr/bin/env python3
"""Bolt Cabinet Lookup - admin CLI (stdlib only, no deps).

Run on the container, from the Proxmox console or `pct enter`:

    python3 server/adminctl.py bootstrap        first super-user, once
    python3 server/adminctl.py add-user         add a coworker
    python3 server/adminctl.py list-users
    python3 server/adminctl.py set-perms <id>
    python3 server/adminctl.py deactivate <id>  reversible; prefer over delete
    python3 server/adminctl.py reset-pin <id>
    python3 server/adminctl.py backup
    python3 server/adminctl.py list-backups
    python3 server/adminctl.py restore <stamp>
    python3 server/adminctl.py export           fold overrides into bins.json

PINs are always prompted for, never taken as an argument -- an argument
would land in the shell history of a shared box. Nothing here ever prints a
PIN, a hash, or the session key.
"""
import sys, os, json, getpass, subprocess, datetime

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


COMMANDS = {
    "bootstrap": cmd_bootstrap, "add-user": cmd_add_user, "list-users": cmd_list_users,
    "set-perms": cmd_set_perms, "deactivate": cmd_deactivate, "reset-pin": cmd_reset_pin,
    "backup": cmd_backup, "list-backups": cmd_list_backups, "restore": cmd_restore,
    "export": cmd_export,
}

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(__doc__)
        sys.exit(0)
    cmd = COMMANDS.get(sys.argv[1])
    if not cmd:
        die(f"unknown command '{sys.argv[1]}'. Try --help.")
    cmd(sys.argv[2:])
