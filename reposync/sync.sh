#!/bin/bash
# apt-mirror trigger loop. The server writes /srv/repo/mirror.list and touches
# /srv/repo/sync.trigger; we sync and report into /srv/repo/status.json.
# The mirrored tree lands in /srv/repo/apt-mirror/mirror/, and a stable
# "mirror" symlink is exposed for nginx.
set -u
REPO=/srv/repo

status() {
    # Strip control chars, quotes and backslashes so the detail embeds safely
    # in the hand-rolled JSON.
    local detail
    detail=$(printf '%s' "$2" | tail -c 1500 | tr '\n' ' ' | tr -d '\000-\037"\\')
    printf '{"state":"%s","last_run":"%s","size":"%s","detail":"%s"}\n' \
        "$1" "$(date -u +%FT%TZ)" \
        "$(du -sh $REPO/apt-mirror/mirror 2>/dev/null | cut -f1)" \
        "$detail" > "$REPO/status.json"
}

mkdir -p "$REPO/apt-mirror/mirror" "$REPO/apt-mirror/skel" "$REPO/apt-mirror/var"
# apt-mirror runs this hook unconditionally; give it a no-op.
[ -f "$REPO/apt-mirror/var/postmirror.sh" ] || printf '#!/bin/sh\n' > "$REPO/apt-mirror/var/postmirror.sh"
chmod +x "$REPO/apt-mirror/var/postmirror.sh"
# nginx serves ./repo/mirror — keep it pointing at apt-mirror's mirror tree.
[ -e "$REPO/mirror" ] || ln -s apt-mirror/mirror "$REPO/mirror"

echo "reposync: waiting for sync triggers"
while true; do
    if [ -f "$REPO/sync.trigger" ]; then
        rm -f "$REPO/sync.trigger"
        if [ ! -s "$REPO/mirror.list" ]; then
            status "failed" "no mirror.list rendered (enable a mirror first)"
            continue
        fi
        echo "reposync: starting apt-mirror run"
        status "running" ""
        out=$(apt-mirror "$REPO/mirror.list" 2>&1)
        rc=$?
        # apt-mirror stages cleanup into a script it expects you to run.
        clean="$REPO/apt-mirror/var/clean.sh"
        [ -x "$clean" ] && "$clean" >/dev/null 2>&1
        if [ $rc -eq 0 ]; then status "done" "$out"; else status "failed" "$out"; fi
        echo "reposync: run finished rc=$rc"
    fi
    sleep 20
done
