#!/usr/bin/env python3
"""
Cybersecurity Research Agent + Excel Generator

Main entry point for the Streamlit web application.
"""

import subprocess
import sys
from pathlib import Path


def main() -> None:
    streamlit_app = Path(__file__).parent / "tui" / "app.py"
    
    if not streamlit_app.exists():
        print(f"Error: {streamlit_app} not found")
        sys.exit(1)
    
    subprocess.run(
        [sys.executable, "-m", "streamlit", "run", str(streamlit_app)],
        check=False
    )


if __name__ == "__main__":
    main()
