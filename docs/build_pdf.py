"""Build a unified PDF from the Sphinx documentation source."""

import os
import signal
import shutil
import subprocess
import sys
from pathlib import Path


DOCS_DIR = Path(__file__).resolve().parent
BUILD_DIR = DOCS_DIR / "_build"
HTML_DIR = BUILD_DIR / "pdf_html"
PDF_DIR = BUILD_DIR / "pdf"
PDF_PATH = PDF_DIR / "sophios-docs.pdf"
CHROME_PROFILE_DIR = BUILD_DIR / "pdf_chrome_profile"


def _chrome_candidates() -> list[Path]:
    candidates: list[Path] = []
    env_chrome = os.environ.get("CHROME_BIN")
    if env_chrome:
        candidates.append(Path(env_chrome))

    for executable in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "chrome"):
        path = shutil.which(executable)
        if path:
            candidates.append(Path(path))

    candidates.extend(
        [
            Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
            Path("/Applications/Chromium.app/Contents/MacOS/Chromium"),
            Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ]
    )
    return candidates


def _find_chrome() -> Path:
    for candidate in _chrome_candidates():
        if candidate.exists():
            return candidate
    raise RuntimeError(
        "Could not find Chrome or Chromium. Install Chrome/Chromium or set CHROME_BIN "
        "to the browser executable before running this PDF build."
    )


def _build_single_html() -> Path:
    env = os.environ.copy()
    env["SOPHIOS_BUILD_PDF"] = "1"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "sphinx",
            "-W",
            "-E",
            "-b",
            "singlehtml",
            "-D",
            "master_doc=pdf_index",
            str(DOCS_DIR),
            str(HTML_DIR),
        ],
        check=True,
        env=env,
    )

    for candidate in (HTML_DIR / "index.html", HTML_DIR / "pdf_index.html"):
        if candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find generated single HTML file in {HTML_DIR}")


def _print_pdf(html_path: Path) -> Path:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    PDF_PATH.unlink(missing_ok=True)
    chrome = _find_chrome()
    command = [
        str(chrome),
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-background-networking",
        "--disable-component-update",
        "--disable-extensions",
        "--disable-sync",
        "--hide-scrollbars",
        f"--user-data-dir={CHROME_PROFILE_DIR}",
        "--no-pdf-header-footer",
        "--print-to-pdf-no-header",
        f"--print-to-pdf={PDF_PATH}",
        html_path.resolve().as_uri(),
    ]
    timeout_seconds = int(os.environ.get("SOPHIOS_PDF_CHROME_TIMEOUT", "30"))
    if os.name == "posix":
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )
    else:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired:
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.terminate()
        stdout, stderr = process.communicate()
        if PDF_PATH.exists() and PDF_PATH.stat().st_size > 0:
            return PDF_PATH
        sys.stderr.write(stderr)
        raise

    if process.returncode != 0:
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        raise subprocess.CalledProcessError(process.returncode, command)
    return PDF_PATH


def main() -> None:
    html_path = _build_single_html()
    pdf_path = _print_pdf(html_path)
    print(pdf_path)


if __name__ == "__main__":
    main()
