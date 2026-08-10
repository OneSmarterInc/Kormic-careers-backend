#!/usr/bin/env python
"""
Django's command-line utility.

DEV SCAFFOLD. This file, `config/`, `devauth/` and `django_api/` exist so the
careers app can be run and demoed on its own. None of them are part of what
ships: careers integrates into the real project, which already has its own
settings, its own accounts app and the real django_api. See README.md.
"""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
    try:
        from django.core.management import execute_from_command_line
    except ImportError as exc:
        raise ImportError(
            "Django is not importable. Is the virtualenv active, and did you "
            "run `pip install -r requirements.txt`?"
        ) from exc
    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
