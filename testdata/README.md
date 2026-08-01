# testdata/

Working directory for locally generated corpora (for example the retrieval
benchmark packed by `ki generate-benchmark testdata/benchmark`). Everything in
here except this file is git-ignored; the directory is mounted read-only into
the containers at `/testdata`.
