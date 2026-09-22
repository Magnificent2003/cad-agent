"""Offline checks for the project-owned converter launch/install scripts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


COMPONENT = Path(__file__).resolve().parents[1]


class ConverterScriptsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cad converter ")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.component = self.root / "cad_converter"
        self.component.mkdir()
        for filename in ("run.sh", "install.sh"):
            shutil.copy2(COMPONENT / filename, self.component / filename)

    def fake_converter(self):
        binary = self.component / "runtime/squashfs-root/usr/bin/ODAFileConverter"
        binary.parent.mkdir(parents=True)
        binary.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "print(json.dumps({'argv': sys.argv[1:], 'library': os.environ['LD_LIBRARY_PATH'], "
            "'plugins': os.environ['QT_PLUGIN_PATH'], 'platform': os.environ['QT_QPA_PLATFORM']}))\n"
        )
        binary.chmod(0o755)

    def test_launcher_resolves_own_directory_and_forwards_arguments(self):
        self.fake_converter()
        env = dict(os.environ, LD_LIBRARY_PATH="/existing/libs", QT_QPA_PLATFORM="custom")
        result = subprocess.run(
            ["bash", str(self.component / "run.sh"), "drawing with spaces", "ACAD2018"],
            cwd="/tmp", env=env, capture_output=True, text=True, check=True,
        )
        actual = json.loads(result.stdout)
        runtime = self.component / "runtime/squashfs-root"
        self.assertEqual(actual["argv"], ["drawing with spaces", "ACAD2018"])
        self.assertEqual(actual["library"], f"{runtime}/usr/lib:{runtime}/usr/bin:/existing/libs")
        self.assertEqual(actual["plugins"], f"{runtime}/usr/plugins")
        self.assertEqual(actual["platform"], "custom")

    def test_existing_install_does_not_require_download_cache(self):
        self.fake_converter()
        result = subprocess.run(
            ["bash", str(self.component / "install.sh")],
            cwd="/tmp", capture_output=True, text=True, check=True,
        )
        self.assertIn("已安装", result.stdout)
        self.assertFalse((self.root / ".cache").exists())

    def test_missing_runtime_explains_installation(self):
        result = subprocess.run(
            ["bash", str(self.component / "run.sh")],
            cwd="/tmp", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("install.sh", result.stderr)

    def test_invalid_cached_archive_stops_before_executing(self):
        cache = self.root / ".cache/downloads"
        cache.mkdir(parents=True)
        (cache / "ODAFileConverter.AppImage").write_text("not an ODA AppImage")
        result = subprocess.run(
            ["bash", str(self.component / "install.sh")],
            cwd="/tmp", capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("SHA-256", result.stderr)
        self.assertFalse((self.component / "runtime/squashfs-root").exists())


if __name__ == "__main__":
    unittest.main()
