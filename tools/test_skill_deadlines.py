# SPDX-FileCopyrightText: 2026 Lusoris <lusoris@pm.me>
# SPDX-License-Identifier: EUPL-1.2
"""Unit tests for CellProfiler and MAGeCK subprocess deadlines."""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path
from typing import Self
from unittest.mock import patch

_TOOLS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _TOOLS_DIR.parent
_INVALID_TIMEOUTS = (0, 0.0, -1, -0.5, float("nan"), float("inf"), -float("inf"), True, False)


class _FallbackArray2D:
    """Minimal 2D pixel array used when numpy is absent from the test venv."""

    def __init__(self, rows: list[list[int]], dtype_name: str) -> None:
        self._rows = rows
        self.ndim = 2
        self.shape = (len(rows), len(rows[0]) if rows else 0)
        self.dtype = dtype_name
        self.flat = [px for row in rows[:256] for px in row[:256]]

    def __eq__(self, other: object) -> _FallbackArray2D:  # type: ignore[override]
        if not isinstance(other, (int, float)):
            return _FallbackArray2D([[0 for _ in row[:256]] for row in self._rows[:256]], "bool")
        return _FallbackArray2D(
            [[1 if px == other else 0 for px in row[:256]] for row in self._rows[:256]],
            "bool",
        )


def _build_fallback_numpy() -> types.ModuleType:
    """Construct a minimal numpy module for manifest TIFF checks without third-party wheels."""
    mod = types.ModuleType("numpy")
    mod.dtype = lambda name: str(name)  # type: ignore[attr-defined]
    mod.ptp = lambda arr: (max(arr.flat) - min(arr.flat)) if arr.flat else 0  # type: ignore[attr-defined]
    mod.iinfo = lambda dt: types.SimpleNamespace(max=255 if str(dt) == "uint8" else 65535)  # type: ignore[attr-defined]
    mod.mean = lambda arr: (sum(arr.flat[:65536]) / len(arr.flat)) if arr.flat else 0.0  # type: ignore[attr-defined]
    return mod


class _FallbackTiffFile:
    """Read minimal uncompressed little-endian baseline grayscale TIFF fixtures."""

    def __init__(self, path: Path) -> None:
        raw = Path(path).read_bytes()
        order, magic, ifd_offset = struct.unpack_from("<2sHI", raw, 0)
        if order != b"II" or magic != 42:
            raise ValueError("Unsupported TIFF byte order or magic")
        (entry_count,) = struct.unpack_from("<H", raw, ifd_offset)
        tags: dict[int, int] = {}
        cursor = ifd_offset + 2
        for _ in range(min(entry_count, 32)):
            tag, field_type, count, val = struct.unpack_from("<HHII", raw, cursor)
            if count == 1 and field_type in (3, 4):
                tags[tag] = val & 0xFFFF if field_type == 3 else val
            cursor += 12
        (next_ifd,) = struct.unpack_from("<I", raw, cursor)
        self._width = tags[256]
        self._height = tags[257]
        self._bits = tags[258]
        self._raw_pixels = raw[tags[273] : tags[273] + tags[279]]
        page = types.SimpleNamespace(photometric=tags[262], samplesperpixel=tags[277])
        self.pages = [page] if next_ifd == 0 else [page, page]
        self.series = [types.SimpleNamespace(levels=[0])]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None

    def asarray(self) -> object:
        """Return a 2D uint8/uint16 array compatible with active numpy."""
        np_mod = sys.modules["numpy"]
        if hasattr(np_mod, "frombuffer"):
            dtype = np_mod.uint8 if self._bits == 8 else np_mod.uint16
            return np_mod.frombuffer(self._raw_pixels, dtype=dtype).reshape((self._height, self._width))
        rows = [
            list(self._raw_pixels[r * self._width : (r + 1) * self._width])
            for r in range(min(self._height, 256))
        ]
        return _FallbackArray2D(rows, "uint8" if self._bits == 8 else "uint16")


def _build_fallback_tifffile() -> types.ModuleType:
    """Construct a minimal tifffile module backed by _FallbackTiffFile."""
    mod = types.ModuleType("tifffile")
    mod.TiffFile = _FallbackTiffFile  # type: ignore[attr-defined]
    return mod


def _write_grayscale_tiff(path: Path, width: int = 16, height: int = 16) -> None:
    """Write a valid uncompressed 16x16 MINISBLACK uint8 TIFF fixture."""
    pixel_bytes = bytes([200] + [12] * (width * height - 1))
    entries = (
        (256, 4, 1, width),
        (257, 4, 1, height),
        (258, 3, 1, 8),
        (259, 3, 1, 1),
        (262, 3, 1, 1),
        (273, 4, 1, 8 + 2 + 9 * 12 + 4),
        (277, 3, 1, 1),
        (278, 4, 1, height),
        (279, 4, 1, len(pixel_bytes)),
    )
    buf = bytearray(struct.pack("<2sHIH", b"II", 42, 8, len(entries)))
    for tag, ftype, count, val in entries[:16]:
        buf.extend(struct.pack("<HHII", tag, ftype, count, val))
    buf.extend(struct.pack("<I", 0))
    buf.extend(pixel_bytes)
    path.write_bytes(bytes(buf))


def _load_script_module(name: str, script_path: Path) -> types.ModuleType:
    """Import a skill helper script by file path without mutating global sys.path."""
    spec = importlib.util.spec_from_file_location(name, script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load module spec from {script_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_executable(path: Path, body: str) -> Path:
    """Write a fast-starting Python stub script and mark it executable."""
    path.write_text(f"#!{sys.executable} -S\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _assert_pid_reaped(testcase: unittest.TestCase, pid_file: Path) -> None:
    """Assert that a child stub recorded a valid PID > 1 and is no longer alive."""
    testcase.assertTrue(pid_file.is_file(), f"Missing child PID file {pid_file}")
    pid = int(pid_file.read_text(encoding="utf-8").strip())
    if pid <= 1:
        raise ValueError(f"Refusing signal check on non-child pid {pid}")
    with testcase.assertRaises(ProcessLookupError):
        os.kill(pid, 0)


class SkillDeadlineTests(unittest.TestCase):
    """Verify positive, timeout and boundary contracts for adopted skill subprocess deadlines."""

    def setUp(self) -> None:
        """Create temporary workspace, install missing imaging stubs, and load helper modules."""
        self._tmp = tempfile.TemporaryDirectory(prefix="skill-deadlines-")
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        injected: dict[str, types.ModuleType] = {}
        try:
            __import__("numpy")
        except ModuleNotFoundError:
            injected["numpy"] = _build_fallback_numpy()
        try:
            __import__("tifffile")
        except ModuleNotFoundError:
            injected["tifffile"] = _build_fallback_tifffile()
        if injected:
            self.enterContext(patch.dict(sys.modules, injected))
        self.cellprofiler = _load_script_module(
            "cellprofiler_nuclei_assay",
            _REPO_ROOT / "skills/cellprofiler/scripts/nuclei_assay.py",
        )
        self.mageck = _load_script_module(
            "mageck_screen_analysis",
            _REPO_ROOT / "skills/mageck/scripts/screen_analysis.py",
        )
        self.pipeline = _REPO_ROOT / "skills/cellprofiler/assets/nuclei.cppipe"

    def _cellprofiler_manifest(self) -> Path:
        """Create a minimal valid microscopy manifest and 16x16 grayscale TIFF."""
        image_path = self.base / "sample_001.tif"
        _write_grayscale_tiff(image_path)
        manifest = self.base / "images.csv"
        manifest.write_text(
            "sample_id,image_path,plate,well,site\nsample_001,sample_001.tif,Plate1,A01,1\n",
            encoding="utf-8",
        )
        return manifest

    def _mageck_counts(self) -> Path:
        """Create a minimal valid pooled-screen count matrix with two replicates per arm."""
        counts = self.base / "counts.tsv"
        counts.write_text(
            "sgRNA\tGene\tc1\tc2\tt1\tt2\n"
            "g1\tGENE1\t100\t110\t20\t22\n"
            "g2\tGENE1\t120\t130\t25\t28\n"
            "g3\tGENE1\t90\t95\t18\t19\n"
            "g4\tGENE2\t100\t105\t100\t104\n"
            "g5\tGENE2\t110\t115\t112\t116\n"
            "g6\tGENE2\t95\t100\t96\t99\n",
            encoding="utf-8",
        )
        return counts

    def test_cellprofiler_positive(self) -> None:
        """Complete a CellProfiler run when the stub writes Complete and valid CSV tables."""
        manifest = self._cellprofiler_manifest()
        stub = _write_executable(
            self.base / "cp_ok.py",
            "import os, sys\n"
            "args = sys.argv[1:]\n"
            "out = args[args.index('-o') + 1]\n"
            "done = args[args.index('--done-file') + 1]\n"
            "open(os.path.join(out, 'Image.csv'), 'w', encoding='utf-8').write(\n"
            "    'ImageNumber,Metadata_Sample,Count_Nuclei\\n1,sample_001,2\\n'\n"
            ")\n"
            "open(os.path.join(out, 'Nuclei.csv'), 'w', encoding='utf-8').write(\n"
            "    'ImageNumber,ObjectNumber,Intensity_MeanIntensity_DNA,'\n"
            "    'Intensity_IntegratedIntensity_DNA,AreaShape_Area\\n'\n"
            "    '1,1,0.25,25.0,100.0\\n1,2,0.50,50.0,120.0\\n'\n"
            ")\n"
            "open(done, 'w', encoding='utf-8').write('Complete\\n')\n",
        )
        output = self.base / "cp_out"
        result = self.cellprofiler.run(manifest, output, str(stub), self.pipeline, timeout=30.0)
        self.assertEqual(result["status"], "complete")
        self.assertEqual(result["measurements"][0]["nuclei"], 2)
        saved = json.loads((output / "assay_qc.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "complete")

    def test_cellprofiler_timeout_kills_child_and_records_failure(self) -> None:
        """Kill a hanging CellProfiler child on deadline, reap it, and persist failed status."""
        manifest = self._cellprofiler_manifest()
        pid_file = self.base / "cp_hang.pid"
        stub = _write_executable(
            self.base / "cp_hang.py",
            f"import os, select, sys\n"
            f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "sys.stdout.write('starting cellprofiler\\n')\n"
            "sys.stdout.flush()\n"
            "select.select([], [], [], 30.0)\n",
        )
        output = self.base / "cp_timeout_out"
        started = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            self.cellprofiler.run(manifest, output, str(stub), self.pipeline, timeout=0.5)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)
        saved = json.loads((output / "assay_qc.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["status"], "failed")
        self.assertIn("0.5", saved["error"])
        self.assertIn("timed out", saved["error"])
        _assert_pid_reaped(self, pid_file)

    def test_cellprofiler_cli_timeout_exits_nonzero(self) -> None:
        """Exit 1 from CellProfiler CLI main() with Error message when --timeout expires."""
        manifest = self._cellprofiler_manifest()
        pid_file = self.base / "cp_cli.pid"
        stub = _write_executable(
            self.base / "cp_cli_hang.py",
            f"import os, select\n"
            f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "select.select([], [], [], 5.0)\n",
        )
        output = self.base / "cp_cli_out"
        stderr = io.StringIO()
        argv = [
            "nuclei_assay.py", "run", str(manifest), str(output),
            "--executable", str(stub), "--pipeline", str(self.pipeline), "--timeout", "0.3",
        ]
        with (
            patch.object(sys, "argv", argv),
            contextlib.redirect_stderr(stderr),
            self.assertRaises(SystemExit) as ctx,
        ):
            self.cellprofiler.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("Error:", stderr.getvalue())
        self.assertIn("0.3", stderr.getvalue())
        _assert_pid_reaped(self, pid_file)

    def test_mageck_positive(self) -> None:
        """Complete a MAGeCK run when --version and test write valid normalization and hits."""
        counts = self._mageck_counts()
        stub = _write_executable(
            self.base / "mageck_ok.py",
            "import sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    sys.stdout.write('0.5.9.5\\n')\n"
            "    sys.exit(0)\n"
            "sys.stderr.write('INFO  @ test: Final size factor: 1.0 1.0 1.0 1.0\\n')\n"
            "open('screen.gene_summary.txt', 'w', encoding='utf-8').write(\n"
            "    'id\\tneg|fdr\\tneg|lfc\\tneg|rank\\tpos|fdr\\tpos|lfc\\tpos|rank\\n'\n"
            "    'GENE1\\t0.01\\t-2.3\\t1\\t0.99\\t-2.3\\t2\\n'\n"
            "    'GENE2\\t0.80\\t0.01\\t2\\t0.50\\t0.01\\t1\\n'\n"
            ")\n",
        )
        output = self.base / "mageck_out"
        report = self.mageck.run_test(
            counts, output, ["c1", "c2"], ["t1", "t2"], executable=str(stub), timeout=30.0,
        )
        self.assertEqual(report["mageck_version"], "0.5.9.5")
        self.assertEqual(report["hits"]["neg"][0]["gene"], "GENE1")
        self.assertTrue((output / "report.json").is_file())

    def test_mageck_timeout_writes_log_kills_child_and_raises(self) -> None:
        """Decode partial POSIX bytes to execution.log, reap child, and raise RuntimeError."""
        counts = self._mageck_counts()
        pid_file = self.base / "mageck_hang.pid"
        stub = _write_executable(
            self.base / "mageck_hang.py",
            f"import os, select, sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    sys.stdout.write('0.5.9.5\\n')\n"
            "    sys.exit(0)\n"
            f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "sys.stdout.write('partial stdout line\\n')\n"
            "sys.stdout.flush()\n"
            "sys.stderr.write('partial stderr line\\n')\n"
            "sys.stderr.flush()\n"
            "select.select([], [], [], 30.0)\n"
            "sys.exit(1)\n",
        )
        output = self.base / "mageck_timeout_out"
        started = time.monotonic()
        with self.assertRaises(RuntimeError) as ctx:
            self.mageck.run_test(
                counts, output, ["c1", "c2"], ["t1", "t2"], executable=str(stub), timeout=0.5,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)
        log_path = output / "execution.log"
        self.assertIn("0.5", str(ctx.exception))
        self.assertIn("timed out", str(ctx.exception))
        self.assertIn(str(log_path), str(ctx.exception))
        self.assertTrue(log_path.is_file())
        log_text = log_path.read_text(encoding="utf-8")
        self.assertIn("partial stdout line", log_text)
        self.assertIn("partial stderr line", log_text)
        _assert_pid_reaped(self, pid_file)

    def test_mageck_version_timeout_is_bounded(self) -> None:
        """Enforce VERSION_TIMEOUT on mageck --version and surface RuntimeError."""
        counts = self._mageck_counts()
        pid_file = self.base / "mageck_ver.pid"
        stub = _write_executable(
            self.base / "mageck_ver_hang.py",
            f"import os, select, sys\n"
            "if '--version' not in sys.argv[1:]:\n"
            "    sys.exit(1)\n"
            f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "select.select([], [], [], 30.0)\n"
            "sys.stdout.write('0.5.9.5\\n')\n",
        )
        output = self.base / "mageck_ver_out"
        started = time.monotonic()
        with (
            patch.object(self.mageck, "VERSION_TIMEOUT", 0.3),
            self.assertRaises(RuntimeError) as ctx,
        ):
            self.mageck.run_test(
                counts, output, ["c1", "c2"], ["t1", "t2"], executable=str(stub), timeout=30.0,
            )
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 10.0)
        self.assertIn("--version", str(ctx.exception))
        self.assertIn("0.3", str(ctx.exception))
        _assert_pid_reaped(self, pid_file)

    def test_invalid_timeouts_rejected_before_process_or_output(self) -> None:
        """Refuse non-positive, non-finite, and boolean timeouts before creating dirs or processes."""
        manifest = self._cellprofiler_manifest()
        counts = self._mageck_counts()
        marker = self.base / "never_started.marker"
        stub = _write_executable(
            self.base / "marker_stub.py",
            f"open({str(marker)!r}, 'w', encoding='utf-8').write('started')\n",
        )
        for index, bad in enumerate(_INVALID_TIMEOUTS[:16]):
            cp_out = self.base / f"cp_bad_{index}"
            with self.assertRaises(ValueError, msg=f"cellprofiler accepted {bad!r}"):
                self.cellprofiler.run(manifest, cp_out, str(stub), self.pipeline, timeout=bad)
            self.assertFalse(cp_out.exists(), f"cellprofiler created output for {bad!r}")
            self.assertFalse(marker.exists(), f"cellprofiler started stub for {bad!r}")

            mg_out = self.base / f"mg_bad_{index}"
            with self.assertRaises(ValueError, msg=f"mageck accepted {bad!r}"):
                self.mageck.run_test(
                    counts, mg_out, ["c1", "c2"], ["t1", "t2"], executable=str(stub), timeout=bad,
                )
            self.assertFalse(mg_out.exists(), f"mageck created output for {bad!r}")
            self.assertFalse(marker.exists(), f"mageck started stub for {bad!r}")

    def test_cli_help_names_seconds_and_mageck_cli_timeout(self) -> None:
        """Expose --timeout in seconds on both CLIs and enforce it via mageck main()."""
        for mod, argv in (
            (self.cellprofiler, ["nuclei_assay.py", "run", "--help"]),
            (self.mageck, ["screen_analysis.py", "--help"]),
        )[:4]:
            stdout = io.StringIO()
            with (
                patch.object(sys, "argv", argv),
                contextlib.redirect_stdout(stdout),
                self.assertRaises(SystemExit) as help_exit,
            ):
                mod.main()
            self.assertEqual(help_exit.exception.code, 0)
            self.assertIn("--timeout", stdout.getvalue())
            self.assertIn("seconds", stdout.getvalue().lower())

        counts = self._mageck_counts()
        pid_file = self.base / "mg_cli.pid"
        stub = _write_executable(
            self.base / "mg_cli_hang.py",
            f"import os, select, sys\n"
            "if '--version' in sys.argv[1:]:\n"
            "    sys.stdout.write('0.5.9.5\\n')\n"
            "    sys.exit(0)\n"
            f"open({str(pid_file)!r}, 'w', encoding='utf-8').write(str(os.getpid()))\n"
            "select.select([], [], [], 5.0)\n",
        )
        out = self.base / "mg_cli_out"
        mg_argv = [
            "screen_analysis.py", "test", "--counts", str(counts), "--control", "c1", "c2",
            "--treatment", "t1", "t2", "--output", str(out), "--mageck", str(stub), "--timeout", "0.3",
        ]
        with patch.object(sys, "argv", mg_argv), self.assertRaises(RuntimeError) as ctx:
            self.mageck.main()
        self.assertIn("0.3", str(ctx.exception))
        _assert_pid_reaped(self, pid_file)


if __name__ == "__main__":
    unittest.main()
