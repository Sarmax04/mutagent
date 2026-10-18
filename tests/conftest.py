import os

# Coverage runs add two pytest runs to every agent loop. Most tests do not need them;
# the ones in test_linecov.py switch coverage back on.
os.environ.setdefault("MUTAGENT_NO_COVERAGE", "1")
