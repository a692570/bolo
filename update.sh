#!/bin/bash
set -u

BOLO_DIR="$(cd "$(dirname "$0")" && pwd)"

result() {
    echo "BOLO_UPDATE_RESULT=$1"
    if [ "${2:-}" != "" ]; then
        echo "BOLO_UPDATE_REASON=$2"
    fi
}

cd "$BOLO_DIR" || {
    result skipped "Bolo directory is unavailable."
    exit 0
}

if [ ! -d .git ]; then
    result skipped "This Bolo install is not a Git checkout."
    exit 0
fi

if ! command -v git >/dev/null 2>&1; then
    result skipped "Git is not installed."
    exit 0
fi

if ! command -v cargo >/dev/null 2>&1; then
    result skipped "Rust Cargo is not installed."
    exit 0
fi

if ! git diff --quiet -- . || ! git diff --cached --quiet -- .; then
    result skipped "Local Bolo files have uncommitted changes. Skipping update."
    exit 0
fi

branch="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ "$branch" = "HEAD" ] || [ "$branch" = "" ]; then
    result skipped "Bolo is on a detached Git commit. Skipping update."
    exit 0
fi

if ! git remote get-url origin >/dev/null 2>&1; then
    result skipped "Bolo has no origin remote configured."
    exit 0
fi

if ! git fetch --quiet origin; then
    result skipped "Could not reach GitHub for updates."
    exit 0
fi

upstream="$(git rev-parse --abbrev-ref --symbolic-full-name '@{u}' 2>/dev/null || true)"
if [ "$upstream" = "" ]; then
    if git rev-parse --verify --quiet "origin/$branch^{commit}" >/dev/null; then
        upstream="origin/$branch"
    elif git rev-parse --verify --quiet "origin/main^{commit}" >/dev/null; then
        upstream="origin/main"
    else
        result skipped "No upstream branch found for updates."
        exit 0
    fi
fi

local_head="$(git rev-parse HEAD)"
remote_head="$(git rev-parse "$upstream")"
if [ "$local_head" = "$remote_head" ]; then
    result current
    exit 0
fi

if ! git merge-base --is-ancestor HEAD "$upstream"; then
    result skipped "Local Bolo is not behind $upstream cleanly. Skipping update."
    exit 0
fi

tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/bolo-update.XXXXXX")"
cleanup() {
    if [ "${worktree_dir:-}" != "" ] && [ -d "$worktree_dir" ]; then
        git worktree remove --force "$worktree_dir" >/dev/null 2>&1 || true
    fi
    rm -rf "$tmp_dir"
}
trap cleanup EXIT

worktree_dir="$tmp_dir/source"
target_dir="$tmp_dir/target"
if ! git worktree add --detach "$worktree_dir" "$upstream" >/dev/null 2>&1; then
    result skipped "Could not prepare the fetched Bolo update."
    exit 0
fi

if ! cargo build --release --manifest-path "$worktree_dir/Cargo.toml" --target-dir "$target_dir"; then
    result skipped "Fetched Bolo update did not build."
    exit 0
fi

if ! git merge --ff-only "$upstream"; then
    result skipped "Could not fast-forward Bolo to $upstream."
    exit 0
fi

new_binary="$BOLO_DIR/target/release/bolo.new"
if ! cp "$target_dir/release/bolo" "$new_binary"; then
    result skipped "Bolo updated, but the new binary could not be installed."
    exit 0
fi
chmod +x "$new_binary"
if ! mv "$new_binary" "$BOLO_DIR/target/release/bolo"; then
    rm -f "$new_binary"
    result skipped "Bolo updated, but the new binary could not be activated."
    exit 0
fi

result updated

# The rebuilt binary has a fresh ad-hoc signature, so macOS may invalidate the
# existing Accessibility grant. Surface this so the user connects "paste stopped
# working after update" with the right fix instead of filing a bug.
osascript -e 'display notification "If paste stops working, re-grant Accessibility in System Settings > Privacy & Security, then run ./restart.sh" with title "Bolo updated" with sound name "Glass"' >/dev/null 2>&1 || true
