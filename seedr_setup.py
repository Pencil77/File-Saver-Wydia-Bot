"""
seedr_setup.py — Token extractor using the 'seedr' pip package.
"""
import getpass
import subprocess
import sys
import os

try:
    # Fix namespace collision: temporarily remove the current directory from sys.path
    # so Python imports the installed pip 'seedr' package instead of your local 'seedr.py'.
    original_path = sys.path.copy()
    if sys.path[0] == '' or sys.path[0] == os.getcwd():
        sys.path.pop(0)
    
    from seedr import SeedrAPI
    
    # Restore the path
    sys.path = original_path

except ImportError:
    print("Installing required packages...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "seedr", "requests"])
    
    # Retry import with the path fix
    if sys.path[0] == '' or sys.path[0] == os.getcwd():
        sys.path.pop(0)
    from seedr import SeedrAPI
    sys.path = original_path

print("=== Seedr Token Extractor ===")
email = input("Seedr Email: ").strip()
password = getpass.getpass("Seedr Password: ")

try:
    # This logs in exactly like your old Colab script
    seedr = SeedrAPI(email=email, password=password)
    
    # The package stores the authorized JWT token here
    token = seedr.token
    
    print(f"\n✅  Your Seedr token:\n\n    {token}\n")
    print("Copy the token above and paste it into your .env using nano:\n")
    print(f"    SEEDR_TOKEN={token}\n")
    
except Exception as e:
    print(f"❌ Login failed: {e}")

