# Mock DMS fixtures

Generate the deterministic fictional law-firm source tree with:

```bash
ki generate-fixtures testdata/generated
```

The command writes `mock_dms/`, `ground-truth.jsonl`, `acl-by-path.json` and
`scenario.json`. The 15-object source contains two isolated matters, a four-version
draft/redline/final/executed M&A chain with real OOXML tracked changes, email threads,
pleadings, cited authority, annexes, an exact duplicate in the wrong folder, an internal
policy and one unsupported poison file that must be quarantined without blocking the
other documents.

All people and companies are fictional. Generated output is ignored by git.
