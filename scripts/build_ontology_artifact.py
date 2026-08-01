"""Build a compact ontology artifact from an OWL file.

The pipeline never parses OWL at runtime; it consumes the JSON artifact this
script emits. The artifact is self-contained and pinned: it records the source
file's sha256 so a deployment can always say exactly which ontology revision
typed its documents.

Usage:
    python scripts/build_ontology_artifact.py LMSS.owl \
        --name lmss --version 2026-07-27 \
        --source-url https://raw.githubusercontent.com/sali-legal/LMSS/main/LMSS.owl \
        --out src/knowledge_index/ontology_data/lmss.json.gz

Node ids are the IRI tails (e.g. 'R8pNPutX0TAJsdFQpJAQV4W'), stable across
artifact rebuilds because they are SALI's own identifiers.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import defaultdict
from pathlib import Path

NS = {
    "owl": "http://www.w3.org/2002/07/owl#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "skos": "http://www.w3.org/2004/02/skos/core#",
}
RDF_ABOUT = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}about"
RDF_RES = "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}resource"

# Facet roots the pipeline can activate, located by their rdfs:label in the
# source ontology. Only `doc_type` is active in v1; the rest ship dormant so
# activating them later is configuration, not an artifact rebuild.
FACET_ROOT_LABELS: dict[str, list[str]] = {
    # Written Asynchronous Communication gives correspondence FORMS (email,
    # letter, fax) a home in the type facet: LMSS models them under
    # Communication Modality, not Document Types, but "what IS this document"
    # for an .eml is: an email.
    "doc_type": ["Document Types", "Knowledge Type", "Written Asynchronous Communication"],
    "area_of_law": ["Area of Law"],
    "service": ["Service"],
    "clause": ["Contractual Clause"],
}


def local_id(iri: str) -> str:
    return iri.rsplit("/", 1)[-1]


def build(owl_path: Path, name: str, version: str, source_url: str | None) -> dict:
    raw = owl_path.read_bytes()
    tree = ET.fromstring(raw)

    labels: dict[str, str] = {}
    parents: dict[str, list[str]] = defaultdict(list)
    definitions: dict[str, str] = {}
    synonyms: dict[str, list[str]] = defaultdict(list)

    for cls in tree.findall("owl:Class", NS):
        iri = cls.get(RDF_ABOUT)
        if not iri:
            continue
        node = local_id(iri)
        label = cls.find("rdfs:label", NS)
        if label is not None and label.text:
            labels[node] = label.text.strip()
        for sub in cls.findall("rdfs:subClassOf", NS):
            parent = sub.get(RDF_RES)
            if parent:
                parents[node].append(local_id(parent))
        definition = cls.find("skos:definition", NS)
        if definition is not None and definition.text:
            definitions[node] = " ".join(definition.text.split())
        for alt in cls.findall("skos:altLabel", NS):
            if alt.text:
                synonyms[node].append(alt.text.strip())

    nodes: dict[str, dict] = {}
    for node, label in labels.items():
        entry: dict = {"l": label}
        node_parents = [p for p in parents.get(node, []) if p in labels]
        if node_parents:
            entry["p"] = node_parents
        if node in definitions:
            entry["d"] = definitions[node]
        if synonyms.get(node):
            entry["s"] = synonyms[node]
        nodes[node] = entry

    by_label: dict[str, list[str]] = defaultdict(list)
    for node, label in labels.items():
        by_label[label].append(node)
    facets: dict[str, list[str]] = {}
    for facet, root_labels in FACET_ROOT_LABELS.items():
        roots = [node for root_label in root_labels for node in by_label.get(root_label, [])]
        if roots:
            facets[facet] = sorted(roots)
        else:
            print(f"warning: no roots found for facet {facet!r} ({root_labels})", file=sys.stderr)

    return {
        "name": name,
        "version": version,
        "source_url": source_url,
        "source_sha256": hashlib.sha256(raw).hexdigest(),
        "facets": facets,
        "nodes": nodes,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("owl", type=Path)
    parser.add_argument("--name", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--source-url", default=None)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    artifact = build(args.owl, args.name, args.version, args.source_url)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(artifact, ensure_ascii=False, separators=(",", ":")).encode()
    if args.out.suffix == ".gz":
        args.out.write_bytes(gzip.compress(payload, mtime=0))
    else:
        args.out.write_bytes(payload)
    print(
        f"wrote {args.out} ({args.out.stat().st_size / 1e6:.1f} MB): "
        f"{len(artifact['nodes'])} nodes, facets {list(artifact['facets'])}"
    )


if __name__ == "__main__":
    main()
