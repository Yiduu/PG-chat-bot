#!/usr/bin/env bash
# render-build.sh
set -e

# Install system dependencies
apt-get update
apt-get install -y build-essential libpq-dev

# Install Python dependencies
pip install -r requirements.txt
