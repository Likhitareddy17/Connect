"""
Shared pytest fixtures and configuration.
"""
import os
import sys

# Ensure the project root is on the path so imports work from any directory
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Prevent any real external calls during tests
os.environ.setdefault("ANTHROPIC_API_KEY", "")
os.environ.setdefault("PROKERALA_CLIENT_ID", "")
os.environ.setdefault("PROKERALA_CLIENT_SECRET", "")
