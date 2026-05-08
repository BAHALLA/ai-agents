import os
import sys

# Make sibling helper modules (e.g. ``eval_metrics``) importable by ADK's
# custom-metric loader, which calls ``importlib.import_module`` on the dotted
# path declared in ``evals/test_config.json``.
sys.path.insert(0, os.path.dirname(__file__))
