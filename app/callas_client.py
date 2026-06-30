"""Обёртка callas pdfToolbox CLI."""
from __future__ import annotations

import fnmatch
import logging
import os
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

CALLAS_HOME = Path(
    os.getenv(
        "CALLAS_HOME",
        "/opt/pdftoolbox/callas_pdfToolboxCLI_x64_Linux_17-0-682",
    )
)
DEFAULT_TIMEOUT = int(os.getenv("CALLAS_TIMEOUT", "180"))


def _lang_args(home: Path) -> list[str]:
    """ru.bin часто отсутствует в CLI-сборке — без файла Callas падает с exit 101."""
    override = os.getenv("CALLAS_LANGUAGE", "").strip()
    if override:
        return [f"--language={override}"]
    if (home / "lang" / "pdfToolbox.ru.bin").is_file():
        return ["--language=ru"]
    if (home / "lang" / "pdfToolbox.en.bin").is_file():
        return ["--language=en"]
    return []


@dataclass
class CallasResult:
    returncode: int
    stdout: str
    stderr: str
    report_path: Path | None = None
    hits: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode < 100

    @property
    def has_warnings(self) -> bool:
        return self.returncode in (2, 7) or any(h.get("severity") == "warning" for h in self.hits)

    @property
    def has_errors(self) -> bool:
        return self.returncode in (3, 8) or any(h.get("severity") == "error" for h in self.hits)

    @property
    def fixups_applied(self) -> bool:
        return self.returncode in (5, 6, 7, 8)


class CallasError(RuntimeError):
    pass


class CallasClient:
    def __init__(self, home: Path | None = None) -> None:
        self.home = Path(home or CALLAS_HOME)
        self.binary = self.home / "pdfToolbox"
        if not self.binary.is_file():
            raise CallasError(f"pdfToolbox не найден: {self.binary}")

    def available(self) -> bool:
        try:
            code, _, _ = self._run(["--help"])
            return code < 100
        except (CallasError, subprocess.SubprocessError, OSError):
            return False

    def find_profile(self, *patterns: str) -> Path | None:
        profiles = self.home / "var" / "Profiles"
        if not profiles.is_dir():
            return None
        all_kfpx = list(profiles.rglob("*.kfpx"))
        for pattern in patterns:
            # точное имя файла
            for path in all_kfpx:
                if path.name == pattern:
                    return path
            # glob по имени (без rglob — иначе ** в *foo* ломает pathlib)
            for path in all_kfpx:
                if fnmatch.fnmatch(path.name, pattern):
                    return path
            if not pattern.startswith("*"):
                wrapped = f"*{pattern}*"
                for path in all_kfpx:
                    if fnmatch.fnmatch(path.name, wrapped):
                        return path
        return None

    def list_key_profiles(self) -> dict[str, str | None]:
        """Проверка наличия ключевых профилей (CLI может быть ok, а профилей — нет)."""
        checks = {
            "bleed_edges": self.find_profile(
                "Generate bleed at page edges.kfpx",
                "*Generate*bleed*edges*",
                "*bleed*edges*",
            ),
            "bleed_upscale": self.find_profile(
                "Generate bleed by upscaling.kfpx",
                "*upscal*bleed*",
                "*bleed*upscal*",
            ),
            "cmyk": self.find_profile(
                "Convert to CMYK only (ISO Coated v2 (ECI)).kfpx", "*CMYK*ISO Coated v2*"
            ),
            "preflight": self.find_profile("Check and fix bleed.kfpx", "*Check*bleed*"),
        }
        return {k: (str(v.relative_to(profiles)) if v else None) for k, v in checks.items()}

    def quick_info(self, pdf: Path) -> str:
        code, out, err = self._run(["--quickpdfinfo", str(pdf)])
        if code >= 100:
            raise CallasError(err or out or f"quickpdfinfo exit {code}")
        return out

    def run_profile(
        self,
        profile: Path,
        pdf: Path,
        *,
        output: Path | None = None,
        analyze_only: bool = False,
        variables: dict[str, str | float] | None = None,
        report_xml: Path | None = None,
        language: str | None = None,
    ) -> CallasResult:
        args: list[str] = []
        if analyze_only:
            args.append("--analyze")
        if language is not None:
            args.append(f"--language={language}")
        else:
            args.extend(_lang_args(self.home))
        if report_xml:
            args.extend(["-r=XML,ALWAYS,PATH=" + str(report_xml)])
        if variables:
            for key, value in variables.items():
                args.append(f"--setvariable={key}:{value}")
        if output:
            args.extend(["-o=" + str(output)])
        args.extend([str(profile), str(pdf)])

        code, out, err = self._run(args)
        hits: list[dict] = []
        if report_xml and report_xml.is_file():
            hits = parse_report_xml(report_xml)
        return CallasResult(code, out, err, report_xml, hits)

    def save_preview(
        self,
        pdf: Path,
        output: Path,
        *,
        page: int = 1,
        pagebox: str = "TRIMBOX",
        width: int = 400,
        height: int = 220,
    ) -> None:
        output.parent.mkdir(parents=True, exist_ok=True)
        before = {p for p in output.parent.glob("*.png")}
        base = output.with_suffix("")

        for box in (pagebox, "CROPBOX", "MEDIABOX", "BLEEDBOX"):
            code, out, err = self._run(
                [
                    "--saveasimg",
                    "--imgformat=PNG",
                    f"--resolution={width}x{height}",
                    f"--pagebox={box}",
                    f"-p={page}",
                    "-o=" + str(base),
                    str(pdf),
                ]
            )
            if self._collect_preview_output(output, base, pdf, before):
                return
            if code < 100 and output.is_file() and output.stat().st_size > 200:
                return

        raise CallasError(err or out or f"saveasimg exit {code}, файл не создан")

    def _collect_preview_output(
        self,
        target: Path,
        base: Path,
        pdf: Path,
        before: set[Path],
    ) -> bool:
        if target.is_file() and target.stat().st_size > 200:
            return True

        patterns = [
            f"{target.name}",
            f"{base.name}*.png",
            f"{base.name}*.PNG",
            f"{pdf.stem}*.png",
        ]
        for pattern in patterns:
            for candidate in sorted(target.parent.glob(pattern)):
                if candidate.stat().st_size > 200:
                    if candidate != target:
                        candidate.replace(target)
                    return True

        new_files = set(target.parent.glob("*.png")) - before
        for candidate in sorted(new_files):
            if candidate.stat().st_size > 200:
                candidate.replace(target)
                return True
        return False

    def save_safety_preview(
        self,
        pdf: Path,
        output: Path,
        *,
        page: int = 1,
        safe_mm: float = 2.0,
        use_bleed: bool = True,
        width: int = 900,
        height: int = 500,
    ) -> None:
        """Превью с линиями реза (зелёная) и безопасной зоны (красная) — как в эталонном UI."""
        output.parent.mkdir(parents=True, exist_ok=True)
        before = {p for p in output.parent.glob("*.png")}
        base = output.with_suffix("")

        args = [
            "--visualizer",
            *_lang_args(self.home),
            f"--part=safety_full",
            "--imgformat=PNG",
            f"--resolution={width}x{height}",
            f"--safetyinside={safe_mm:g}mm",
            f"-p={page}",
            "-o=" + str(base),
            str(pdf),
        ]
        if use_bleed:
            args.insert(2, "--usebleed")

        code, out, err = self._run(args)
        if self._collect_preview_output(output, base, pdf, before):
            return
        if code < 100 and output.is_file() and output.stat().st_size > 200:
            return
        raise CallasError(err or out or f"visualizer safety_full exit {code}")

    def safety_report(self, pdf: Path, output: Path, page: int = 1) -> None:
        code, out, err = self._run(
            [
                "--visualizer",
                *_lang_args(self.home),
                "--part=safety_full",
                "--format=pdfreport",
                "--resolution=150",
                f"-p={page}",
                "-o=" + str(output),
                str(pdf),
            ]
        )
        if code >= 100:
            logger.warning("safety_report: %s", err or out)

    def _run(self, args: list[str]) -> tuple[int, str, str]:
        cmd = [str(self.binary), *args]
        logger.info("callas: %s", " ".join(cmd[:6]) + ("..." if len(cmd) > 6 else ""))
        try:
            proc = subprocess.run(
                cmd,
                cwd=self.home,
                capture_output=True,
                text=True,
                timeout=DEFAULT_TIMEOUT,
            )
        except subprocess.TimeoutExpired as e:
            raise CallasError(f"Callas timeout ({DEFAULT_TIMEOUT}s)") from e
        return proc.returncode, proc.stdout, proc.stderr


def parse_report_xml(path: Path) -> list[dict]:
    hits: list[dict] = []
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError:
        return hits

    for hit in root.iter("hit"):
        hits.append(
            {
                "severity": (hit.get("severity") or hit.findtext("severity") or "info").lower(),
                "message": hit.findtext("message") or hit.findtext("text") or hit.get("message") or "",
                "page": hit.get("page") or hit.findtext("page"),
            }
        )
    if hits:
        return hits

    for problem in root.iter("problem"):
        hits.append(
            {
                "severity": (problem.get("type") or problem.get("severity") or "warning").lower(),
                "message": problem.findtext("message") or problem.get("message") or "",
                "page": problem.get("page"),
            }
        )
    return hits
