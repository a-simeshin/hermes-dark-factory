"""Project settings for the factory scripts.

The scripts are a generic engine; everything that names one project (the
repository, the board, the owner) lives in one file, ``$HERMES_HOME/factory.env``
(plain ``KEY=VALUE`` lines, ``#`` comments). An environment variable with the
same name wins over the file, so a test or a one-off run can override a value.

    from factory_config import cfg, cfg_list, hermes_home
    REPO = cfg("FACTORY_REPO")                # required: exits if missing
    BOARD = cfg("FACTORY_BOARD", "main")      # optional with a default

Shell scripts read the same file with ``. "$HERMES_HOME/factory.env"``.
"""
import os
import sys


def hermes_home():
    return os.environ.get("HERMES_HOME") or os.path.expanduser("~/.hermes")


def _file_values():
    values = {}
    try:
        with open(os.path.join(hermes_home(), "factory.env"), encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    except OSError:
        pass
    return values


_MISSING = object()


def cfg(key, default=_MISSING):
    """Value of ``key``: environment first, then factory.env, then ``default``.

    Without a default a missing key stops the script with a message that names
    the key: a script silently working on the wrong repository is worse than a
    script that refuses to start."""
    value = os.environ.get(key) or _file_values().get(key)
    if value:
        return os.path.expanduser(value) if value.startswith("~") else value
    if default is _MISSING:
        sys.exit("%s is not set: add it to %s"
                 % (key, os.path.join(hermes_home(), "factory.env")))
    return default


def cfg_list(key, default=()):
    """Comma-separated value as a list."""
    raw = cfg(key, "")
    return [p.strip() for p in raw.split(",") if p.strip()] or list(default)


if __name__ == "__main__":
    import tempfile
    with tempfile.TemporaryDirectory() as home:
        os.environ["HERMES_HOME"] = home
        with open(os.path.join(home, "factory.env"), "w") as fh:
            fh.write('# comment\nFACTORY_REPO="o/r"\nFACTORY_BASE_BRANCHES=main, dev\n'
                     "FACTORY_REPO_PATH=~/p\n")
        assert cfg("FACTORY_REPO") == "o/r"
        assert cfg_list("FACTORY_BASE_BRANCHES") == ["main", "dev"]
        assert cfg("FACTORY_REPO_PATH") == os.path.expanduser("~/p")
        assert cfg("FACTORY_BOARD", "main") == "main"
        os.environ["FACTORY_REPO"] = "env/wins"
        assert cfg("FACTORY_REPO") == "env/wins"
        try:
            cfg("FACTORY_NOPE")
        except SystemExit as exc:
            assert "FACTORY_NOPE" in str(exc)
        else:
            raise AssertionError("missing required key must exit")
    print("factory_config: ok")
