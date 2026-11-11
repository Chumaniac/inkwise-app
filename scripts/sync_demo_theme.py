"""Keep the standalone demo's theme bundle identical to the workspace source."""
import argparse
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
FILES=('theme.css','assets/mark.svg','assets/fonts/inter-latin-wght-normal.woff2','assets/fonts/OFL-Inter.txt','assets/fonts/NOTICE.md','assets/fonts/jetbrains-mono-latin-wght-normal.woff2','assets/fonts/OFL-JetBrains-Mono.txt')


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--check',action='store_true');args=parser.parse_args()
    for name in FILES:
        source=ROOT/'web'/name;target=ROOT/'demo/dist'/name
        expected=source.read_bytes()
        if args.check:
            if not target.is_file() or target.read_bytes()!=expected:raise SystemExit('Theme asset out of sync: '+name)
        else:
            target.parent.mkdir(parents=True,exist_ok=True)
            if not target.exists() or target.read_bytes()!=expected:target.write_bytes(expected)
    print('Shared light theme and local font assets verified' if args.check else 'Standalone demo theme synchronized')


if __name__=='__main__':main()
