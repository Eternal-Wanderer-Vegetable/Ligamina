#!/usr/bin/env bash
set -eu

repository_path="${1:?pass the mounted repository path}"
probe_root="$(mktemp -d /tmp/ligamina-userns-boundary.XXXXXX)"
trap 'rm -rf "$probe_root"' EXIT

unshare --user --map-root-user --mount --net --pid --fork --mount-proc sh -c '
set -eu
repository_path="$1"
probe_root="$2"
mount --make-rprivate /
mkdir -p "$probe_root/repository"
mount --bind "$repository_path" "$probe_root/repository"
mount -o remount,bind,ro "$probe_root/repository"
if mount -o remount,bind,rw "$probe_root/repository" 2>/dev/null; then
  echo remount_rw_allowed
  mount -o remount,bind,ro "$probe_root/repository"
else
  echo remount_rw_denied
fi
mount -t tmpfs tmpfs /mnt
if test -e "$repository_path/README.md"; then echo repo_visible; else echo repo_hidden; fi
' _ "$repository_path" "$probe_root"
