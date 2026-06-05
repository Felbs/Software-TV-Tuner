"""Pure-Python unit tests for STVT helper tools.

These tests do NOT need the SDR, GNU Radio, or any of the chain.
They cover scan.json parsing, capacity math, EPG rendering, and
schedule queue operations — code that runs entirely on the JSON
artifacts the picker produces.

Run with:
    python -m unittest discover tools/tests -v

or, since pytest works out of the box on unittest tests:
    pytest tools/tests/ -v
"""
