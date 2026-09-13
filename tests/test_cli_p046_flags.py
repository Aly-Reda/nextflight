"""
Tests for the CLI flags exposing v0.4.2-0.4.6 features:
--locale, --meta-tags, --api-routes, --base-path, --error-digest,
--version-hint, --pagination.
"""
import json
import subprocess
import sys


def _run_cli(*args):
    return subprocess.run(
        [sys.executable, "-m", "nextflight.cli", *args],
        capture_output=True, text=True, check=True,
    )


def test_cli_locale(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<html lang="fr"><body>Bonjour</body></html>')
    result = _run_cli(str(html_file), "--locale")
    assert json.loads(result.stdout) == "fr"


def test_cli_locale_none(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text("<html><body>hi</body></html>")
    result = _run_cli(str(html_file), "--locale")
    assert json.loads(result.stdout) is None


def test_cli_meta_tags(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<meta property="og:title" content="Cool Widget">')
    result = _run_cli(str(html_file), "--meta-tags")
    assert json.loads(result.stdout) == {"og:title": "Cool Widget"}


def test_cli_api_routes(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>fetch("/api/products")</script>')
    result = _run_cli(str(html_file), "--api-routes")
    assert json.loads(result.stdout) == ["/api/products"]


def test_cli_base_path(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script src="/docs/_next/static/chunks/1.js"></script>')
    result = _run_cli(str(html_file), "--base-path")
    assert json.loads(result.stdout) == "/docs"


def test_cli_error_digest(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>{"digest":"abc123"}</script>')
    result = _run_cli(str(html_file), "--error-digest")
    assert json.loads(result.stdout) == "abc123"


def test_cli_version_hint(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>')
    result = _run_cli(str(html_file), "--version-hint")
    parsed = json.loads(result.stdout)
    assert "range" in parsed
    assert "bundler" in parsed


def test_cli_pagination(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text(
        '<script>self.__next_f.push([1, '
        '"1:{\\"hasNextPage\\":true,\\"endCursor\\":\\"c2\\"}"'
        '])</script>'
    )
    result = _run_cli(str(html_file), "--pagination")
    assert json.loads(result.stdout) == {"hasNextPage": True, "endCursor": "c2"}


def test_cli_pagination_none(tmp_path):
    html_file = tmp_path / "page.html"
    html_file.write_text('<script>self.__next_f.push([1, "1:{\\"a\\":1}"])</script>')
    result = _run_cli(str(html_file), "--pagination")
    assert json.loads(result.stdout) is None
