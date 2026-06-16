#!/bin/bash
# xchtml installer
# Usage: curl -fsSL https://raw.githubusercontent.com/igram7/xchtml/main/install.sh | bash

set -e

echo "🔧 Installing xchtml..."

if ! command -v brew &>/dev/null; then
    echo "❌ Homebrew not found. Install it first: https://brew.sh"
    exit 1
fi

brew tap igram7/xchtml 2>/dev/null || true
brew trust igram7/xchtml 2>/dev/null || true
brew install xchtml

echo ""
echo "✅ xchtml installed successfully!"
echo "   Run: xchtml generate"
