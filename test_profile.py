#!/usr/bin/env python3
"""Test script to verify UnattendedBot8300 profile configuration."""

import subprocess
import sys
import os

# Set HERMES_HOME
os.environ['HERMES_HOME'] = r'C:\Users\pc\.hermes'

# Path to hermes CLI
hermes_path = r'C:\Users\pc\AppData\Local\hermes\hermes-agent\venv\Scripts\hermes.exe'

# Check profile exists
print("=== Checking UnattendedBot8300 Profile ===")
result = subprocess.run([hermes_path, 'profile', 'list'], capture_output=True, text=True)
print(result.stdout)
print(result.stderr)

# Check skills
print("\n=== Checking Skills ===")
result = subprocess.run([hermes_path, 'skills', 'list'], capture_output=True, text=True)
print(result.stdout)
# Look for unattendedbot8300 in output
if 'unattendedbot8300' in result.stdout.lower():
    print("SUCCESS: unattendedbot8300 skill found!")
else:
    print("WARNING: unattendedbot8300 skill not found in list")

# Check skill directory
print("\n=== Checking Skill Directory ===")
skill_dir = r'C:\Users\pc\.hermes\profiles\unattendedbot8300\skills\unattendedbot8300'
if os.path.exists(skill_dir):
    print(f"Skill directory exists: {skill_dir}")
    for f in os.listdir(skill_dir):
        print(f"  - {f}")
else:
    print(f"Skill directory NOT found: {skill_dir}")