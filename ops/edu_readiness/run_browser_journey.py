"""Build this checkout and run the disposable Education browser journeys."""
import argparse
import subprocess
import sys
from pathlib import Path
from defusedxml import ElementTree

ROOT = Path(__file__).resolve().parents[2]


def remove_host_metadata(junit):
    """Keep assertion evidence without identifying the developer machine."""
    tree = ElementTree.parse(junit)
    for node in tree.iter():
        node.attrib.pop('hostname', None)
    tree.write(junit, encoding='utf-8', xml_declaration=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run-dir', type=Path, required=True)
    args = parser.parse_args()
    evidence = args.run_dir.resolve() / 'raw' / 'browser_journey'
    # Never certify a stale build or JUnit file from an earlier invocation.
    evidence.mkdir(parents=True, exist_ok=False)
    with (evidence / 'build.log').open('w') as log:
        built = subprocess.run(['npm', '--prefix', 'frontend', 'run', 'build'],
                               cwd=ROOT, stdout=log, stderr=subprocess.STDOUT)
    if built.returncode:
        print('Current-checkout frontend build failed.')
        return built.returncode
    result = subprocess.run([sys.executable, '-m', 'pytest', 'tests/education_browser',
                           '-q', '--tb=no', '-o', 'xfail_strict=true',
                           '--junitxml=' + str(evidence / 'junit.xml')], cwd=ROOT)
    junit = evidence / 'junit.xml'
    if not junit.exists():
        print('Browser journey produced no JUnit evidence.')
        return 1
    remove_host_metadata(junit)
    return result.returncode


if __name__ == '__main__':
    raise SystemExit(main())
