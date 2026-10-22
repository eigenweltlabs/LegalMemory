"""Bootstrap a hash-pinned pip without trusting an older pip's CA bundle.

Runtime images pass a Docker ADD --checksum wheel. CI downloads the same exact
wheel with curl. Modern ensurepip/virtualenv seeds are replaced; legacy Python
3.8/3.9 compatibility seeds retain their version and receive only the fixed CA.
"""
import argparse
import ast
import base64
import csv
import hashlib
import importlib.util
import io
import json
import os
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import zipfile
from pathlib import Path

VERSION = "26.2.2"
WHEEL_NAME = f"pip-{VERSION}-py3-none-any.whl"
SHA256 = "fd7f4dfa8190e014013b9f08f3e973f4e4c26ffd179a204895533a7254813a70"
URL = "https://files.pythonhosted.org/packages/3f/06/3e4420302363fbb71be3c7f35ce042f54bfca330e3e66b3a2e3c4cca579f/" + WHEEL_NAME
ROOT_NAME = b"emSign ECC Root CA - G3"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def repair_legacy_wheel(path, fixed_ca):
    """Patch only a confirmed affected vendored bundle and regenerate RECORD."""
    with zipfile.ZipFile(path) as archive:
        files = {item.filename: archive.read(item) for item in archive.infolist()}
    bundle = "pip/_vendor/certifi/cacert.pem"
    if bundle not in files or ROOT_NAME not in files[bundle]:
        return None
    before = digest(path)
    files[bundle] = fixed_ca
    record = next(name for name in files if name.endswith(".dist-info/RECORD"))
    output = io.StringIO()
    writer = csv.writer(output, lineterminator="\n")
    for name, content in files.items():
        if name != record:
            encoded = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
            writer.writerow([name, "sha256=" + encoded, len(content)])
    writer.writerow([record, "", ""])
    files[record] = output.getvalue().encode()
    temporary = path.with_suffix(".whl.tmp")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in files.items():
            archive.writestr(name, content)
    temporary.chmod(path.stat().st_mode)
    temporary.replace(path)
    return {"path": str(path), "kind": "legacy-ca-only", "before_sha256": before, "after_sha256": digest(path), "record_regenerated": True}


def refresh_seeds(wheel, fixed_ca):
    reports = []
    stdlib = Path(sysconfig.get_path("stdlib"))
    ensurepip = stdlib / "ensurepip"
    wheel_dirs = {ensurepip / "_bundled"}
    configured = sysconfig.get_config_var("WHEEL_PKG_DIR")
    if configured:
        wheel_dirs.add(Path(configured))
    # Distribution wheels may also support the old system interpreter.
    if Path(sys.base_prefix) == Path("/usr"):
        wheel_dirs.update(Path("/usr/share").glob("python*-wheels"))
    for directory in wheel_dirs:
        old = list(directory.glob("pip-*.whl")) if directory.is_dir() else []
        if not old:
            continue
        if not os.access(directory, os.W_OK):
            reports.append({"path": str(directory), "kind": "read-only-base-seeds", "action": "patch the interpreter image as its administrator"})
            continue
        legacy = "python3-wheels" == directory.name and sys.version_info >= (3, 10)
        if legacy:
            for seed in old:
                report = repair_legacy_wheel(seed, fixed_ca)
                if report:
                    reports.append(report)
            continue
        target = directory / WHEEL_NAME
        shutil.copyfile(wheel, target)
        for seed in old:
            if seed != target:
                seed.unlink()
        reports.append({"path": str(target), "kind": "pip-upgrade", "sha256": digest(target)})
    source = ensurepip / "__init__.py"
    if source.is_file() and os.access(source, os.W_OK):
        content = source.read_text()
        content = re.sub(r'(_PIP_VERSION\s*=\s*)["\x27][^"\x27]+["\x27]', lambda match: match[1] + repr(VERSION), content)
        source.write_text(content)
    embed = Path(sysconfig.get_path("purelib")) / "virtualenv/seed/wheels/embed"
    source = embed / "__init__.py"
    if source.is_file():
        content = source.read_text()
        tree = ast.parse(content)
        assignments = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
                name = node.targets[0].id
                if name in {"BUNDLE_SUPPORT", "BUNDLE_SHA256"}:
                    assignments[name] = (node, ast.literal_eval(node.value))
        support = assignments["BUNDLE_SUPPORT"][1]
        hashes = assignments.get("BUNDLE_SHA256", (None, {}))[1]
        for python_version, packages in support.items():
            if "pip" not in packages:
                continue
            if tuple(map(int, python_version.split("."))) >= (3, 10):
                packages["pip"] = WHEEL_NAME
            else:
                seed = embed / packages["pip"]
                report = repair_legacy_wheel(seed, fixed_ca)
                if report:
                    reports.append(report)
                hashes[seed.name] = digest(seed)
        shutil.copyfile(wheel, embed / WHEEL_NAME)
        hashes[WHEEL_NAME] = SHA256
        used = {packages.get("pip") for packages in support.values()}
        for seed in embed.glob("pip-*.whl"):
            if seed.name not in used:
                seed.unlink()
                hashes.pop(seed.name, None)
        lines = content.splitlines(keepends=True)
        for name, (node, value) in sorted(assignments.items(), key=lambda item: item[1][0].lineno, reverse=True):
            lines[node.lineno - 1:node.end_lineno] = [name + " = " + repr(value) + "\n"]
        source.write_text("".join(lines))
        reports.append({"path": str(embed / WHEEL_NAME), "kind": "virtualenv-pip-upgrade", "sha256": SHA256})
    return reports


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", nargs="?")
    parser.add_argument("--replace-system-pip", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 10):  # noqa: UP036 - standalone bootstrap also checks distro Python
        raise SystemExit("pip 26.2.2 requires Python 3.10 or newer")
    with tempfile.TemporaryDirectory(prefix="secure-pip-") as temporary:
        wheel = Path(args.wheel) if args.wheel else Path(temporary) / WHEEL_NAME
        if not args.wheel:
            subprocess.run(["curl", "--fail", "--silent", "--show-error", "--location", URL, "--output", str(wheel)], check=True)
        if digest(wheel) != SHA256:
            raise SystemExit("Pinned pip wheel SHA-256 mismatch")
        if wheel.name != WHEEL_NAME:
            renamed = Path(temporary) / WHEEL_NAME
            shutil.copyfile(wheel, renamed)
            wheel = renamed
        command = [sys.executable, "-I", str(wheel) + "/pip", "install", "--no-index", "--no-deps", "--disable-pip-version-check", "--upgrade"]
        target = Path(sysconfig.get_path("purelib"))
        if args.replace_system_pip:
            # RPM Python excludes /usr/local under -I; update its real import path.
            target = Path(importlib.util.find_spec("pip").origin).parent.parent
            command.extend(["--ignore-installed", "--target", str(target)])
        subprocess.run(command + [str(wheel)], check=True)
        if args.replace_system_pip:
            for metadata in target.glob("pip-*.dist-info"):
                if metadata.name != f"pip-{VERSION}.dist-info":
                    shutil.rmtree(metadata)
        with zipfile.ZipFile(wheel) as archive:
            fixed_ca = archive.read("pip/_vendor/certifi/cacert.pem")
        if ROOT_NAME in fixed_ca:
            raise SystemExit("Pinned pip wheel unexpectedly contains the distrusted root")
        seeds = refresh_seeds(wheel, fixed_ca)
        subprocess.run([sys.executable, "-I", "-c", "import pip,ssl; from pip._vendor import certifi; assert pip.__version__ == '26.2.2'; assert not any('emSign ECC Root CA - G3' in str(c['subject']) for c in ssl.create_default_context(cafile=certifi.where()).get_ca_certs()); print('pip 26.2.2 and vendored CA trust verified')"], check=True)
        print(json.dumps({"pip_version": VERSION, "bootstrap_sha256": SHA256, "seeds": seeds}, sort_keys=True))


if __name__ == "__main__":
    main()
