#!/bin/sh
# The runner accepts a job hook only when its path ends in .sh. The check is fork_guard.py, beside this file.
exec /usr/bin/python3 "$(dirname "$0")/fork_guard.py"
