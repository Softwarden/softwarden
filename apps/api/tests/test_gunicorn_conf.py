"""gunicorn.conf.py's logging. Gunicorn turns its access log on whenever `logconfig_dict` is
set, whatever `accesslog` says, so the dictionary itself has to silence `gunicorn.access`: otherwise every request is
logged twice, the second time with its Referer and User-Agent and without the scrubber the app's own line goes through.
Gunicorn itself doesn't import on Windows (fcntl), so this applies the dictionary the way gunicorn's Logger.setup does:
its CONFIG_DEFAULTS updated with ours, then dictConfig."""

import io
import logging
import logging.config
from contextlib import redirect_stdout
from pathlib import Path

import pytest

CONF = Path(__file__).resolve().parent.parent / "gunicorn.conf.py"
LOGGERS = ("gunicorn.access", "gunicorn.error")


def gunicorn_defaults() -> dict:
    try:
        from gunicorn.glogging import CONFIG_DEFAULTS  # type: ignore[import-untyped]
    except ImportError:  # Windows: gunicorn needs fcntl
        return {}
    return dict(CONFIG_DEFAULTS)


@pytest.fixture
def gunicorn_logging(monkeypatch):
    """Applies gunicorn.conf.py's logconfig_dict; puts the two gunicorn loggers back afterwards."""
    saved = {
        name: (lg.handlers[:], lg.level, lg.propagate, lg.disabled)
        for name in LOGGERS
        for lg in [logging.getLogger(name)]
    }
    root = logging.getLogger()
    saved_root = (root.handlers[:], root.level)

    def apply(log_format: str = "json") -> dict:
        monkeypatch.setenv("SOFTWARDEN_LOG_FORMAT", log_format)
        conf: dict = {}
        exec(CONF.read_text(encoding="utf-8"), conf)  # noqa: S102
        config = gunicorn_defaults()
        config.update(conf["logconfig_dict"])
        config.pop("root", None)  # gunicorn's own root handler would leak into the rest of the test run
        logging.config.dictConfig(config)
        return conf

    yield apply
    for name, (handlers, level, propagate, disabled) in saved.items():
        lg = logging.getLogger(name)
        lg.handlers[:] = handlers
        lg.setLevel(level)
        lg.propagate = propagate
        lg.disabled = disabled
    root.handlers[:] = saved_root[0]
    root.setLevel(saved_root[1])


@pytest.mark.parametrize("log_format", ["json", "text"])
def test_gunicorn_writes_no_access_line(gunicorn_logging, log_format):
    out = io.StringIO()
    with redirect_stdout(out):  # the console handler resolves ext://sys.stdout when it is configured
        conf = gunicorn_logging(log_format)
        access = logging.getLogger("gunicorn.access")
        # What gunicorn's Logger.access() does for each request.
        access.info(
            '%(h)s "%(r)s" %(s)s "%(a)s"', {"h": "10.0.0.1", "r": "GET /readyz/web", "s": 200, "a": "UA Bearer sw_x"}
        )
        logging.getLogger("gunicorn.error").info("Booting worker with pid: 7")
    written = out.getvalue()
    assert conf["accesslog"] is None
    assert "readyz" not in written and "Bearer" not in written, written
    assert not access.propagate  # nor through the root logger
    assert "Booting worker" in written  # gunicorn's own lines still go out
