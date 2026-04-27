"""Quick smoke test — verify detection + mapping for each fixture."""
import glob
import os
import sys

from corpus_intel.core.ingest import quality_flags, read_upload
from corpus_intel.core.schema import suggest_mapping
from corpus_intel.core.sources import detect_all, get_adapter


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    fixtures = sorted(glob.glob(os.path.join(here, "fixtures", "test_*.csv")))
    lines = []
    for path in fixtures:
        with open(path, "rb") as f:
            blob = f.read()
        name = os.path.basename(path)
        r = read_upload(name, blob)
        cands = detect_all(r.df, name)
        top = cands[0]
        adapter = get_adapter(top["id"])
        pre_wired = adapter.CANONICAL_COLUMNS if adapter else None
        sugg = suggest_mapping(list(r.df.columns), pre_wired=pre_wired)
        mapping = {src: info["canonical"] for src, info in sugg.items() if info["canonical"]}
        flags = quality_flags(r.df, mapping)
        lines.append(
            f"{name:25s} -> {top['id']:10s} conf={top['confidence']:.2f}  "
            f"cols={len(r.df.columns)} mapped={len(mapping)}  flags={flags}"
        )
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))


if __name__ == "__main__":
    main()
