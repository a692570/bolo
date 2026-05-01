#!/bin/bash
set -e

echo "Installing Bolo - voice dictation for macOS"
echo "============================================"

if ! command -v cargo &>/dev/null; then
  echo "ERROR: Rust/Cargo is required. Install from https://rustup.rs"
  exit 1
fi

echo "Building Rust binary..."
cargo build --release

if [ -z "$TELNYX_API_KEY" ]; then
  echo ""
  echo "You need a Telnyx API key to use Bolo."
  echo "Get one at https://telnyx.com"
  echo ""
  read -p "Paste your TELNYX_API_KEY here: " key
  echo ""
  echo "Adding to ~/.bolo/env..."
  mkdir -p ~/.bolo
  touch ~/.bolo/env
  if grep -q '^TELNYX_API_KEY=' ~/.bolo/env; then
    sed -i.bak "s|^TELNYX_API_KEY=.*|TELNYX_API_KEY=\"$key\"|" ~/.bolo/env
  else
    echo "TELNYX_API_KEY=\"$key\"" >> ~/.bolo/env
  fi
fi

echo ""
echo "Done. Run Bolo with: target/release/bolo"
echo ""
echo "Grant two permissions when prompted or in System Settings:"
echo " 1. Accessibility"
echo " 2. Microphone"
echo ""
echo "Usage: Hold Right Option anywhere to dictate. Release to transcribe and paste."
echo "Logs: tail -f /tmp/bolo.log"
